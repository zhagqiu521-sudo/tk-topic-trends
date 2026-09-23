#!/usr/bin/env python3
"""TikHub backfill — 3x/day full 48h sweep, merges into the shared registry.

tikwm (30-min primary) suffers shared-IP quota exhaustion; this sweep uses TikHub
(per-account billing, $0.001/req) to catch whatever the primary missed.
Per topic: paginate until a page yields <5 in-window items (or hard cap).
Anchors come in the list response — no page checks needed.
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

API = "https://api.tikhub.io/api/v1/tiktok/app/v3/fetch_hashtag_video_list"
CHALLENGES = {
    "capcut": "1663935709411330",
    "capcutpioneer": "7356025154310733831",
    "capcutnow": "1684704995991554",
    "hypic": "1667855826908166",
    "hypiccreator": "7234524314999980059",
    "godpic": "1657530774358033",
}
PRODUCTS = {
    "capcut": {"label": "CapCut（剪映）", "tags": ["capcut", "capcutpioneer", "capcutnow"]},
    "hypic": {"label": "Hypic（醒图）", "tags": ["hypic", "hypiccreator", "godpic"]},
}
COUNT_PER_PAGE = 20
MAX_PAGES = 15            # hard cap per topic (safety)
STOP_WHEN_FRESH_BELOW = 5 # page yields fewer in-window items → topic done
MAX_AGE_HOURS = 48
SLEEP_BETWEEN_REQ = 0.5   # TikHub allows 10/s

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fetch_tikhub(ch_id, cursor):
    token = os.environ.get("TIKHUB_TOKEN", "")
    if not token:
        raise RuntimeError("TIKHUB_TOKEN not set")
    url = f"{API}?ch_id={ch_id}&count={COUNT_PER_PAGE}&cursor={cursor}&region=US"
    out = subprocess.run(
        ["curl", "-s", "-m", "30", "--compressed",
         "-H", f"Authorization: Bearer {token}",
         "-H", f"User-Agent: {UA}",
         "-H", "Accept: application/json",
         url],
        capture_output=True, text=True, timeout=45, check=True)
    return json.loads(out.stdout)


def map_video(x, tag):
    st = x.get("statistics") or {}
    anchors = x.get("anchors") or []
    kws = [a.get("keyword", "") for a in anchors if a.get("keyword")]
    return {
        "video_id": str(x.get("aweme_id") or st.get("aweme_id") or ""),
        "author": (x.get("author") or {}).get("unique_id", ""),
        "title": (x.get("desc") or "")[:120],
        "digg": st.get("digg_count", 0),
        "collect": st.get("collect_count", 0),
        "comment": st.get("comment_count", 0),
        "share": st.get("share_count", 0),
        "play": st.get("play_count", 0),
        "created_at": x.get("create_time", 0),
        "linked": bool(kws),
        "anchor": " | ".join(dict.fromkeys(kws)),
        "topics": [tag],
    }


def load_json_file(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


# ---- at-rest encryption (same format as collect.py): [16B IV][AES-256-CBC] ----
def data_key():
    k = os.environ.get("DATA_KEY", "")
    try:
        return bytes.fromhex(k) if k else None
    except ValueError:
        return None


def enc_write(path, data_bytes, key):
    if not key:
        with open(path, "wb") as f:
            f.write(data_bytes)
        return
    iv = os.urandom(16)
    out = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-K", key.hex(), "-iv", iv.hex()],
                         input=data_bytes, capture_output=True, check=True)
    with open(path + ".enc", "wb") as f:
        f.write(iv + out.stdout)


def dec_read(path, key):
    raw = open(path, "rb").read()
    if not key:
        return raw
    out = subprocess.run(["openssl", "enc", "-d", "-aes-256-cbc", "-K", key.hex(), "-iv", raw[:16].hex()],
                         input=raw[16:], capture_output=True, check=True)
    return out.stdout


def store_json(path, obj, key):
    enc_write(path, json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8"), key)


def read_json(path, key, default):
    enc_path = path + ".enc"
    if os.path.exists(enc_path):
        src, use_key = enc_path, key          # encrypted: decrypt
    elif os.path.exists(path):
        src, use_key = path, None             # legacy plaintext: read as-is
    else:
        return default
    try:
        return json.loads(dec_read(src, use_key).decode("utf-8"))
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError):
        return default


def main():
    now = datetime.now(timezone.utc)
    cutoff = int((now - timedelta(hours=MAX_AGE_HOURS)).timestamp())
    reg_path = "data/registry.json"
    DK = data_key()
    registry = read_json(reg_path, DK, {})

    new_total, req_total = 0, 0
    for tag, cid in CHALLENGES.items():
        fresh_total, empty_pages, pages = 0, 0, 0
        for p in range(MAX_PAGES):
            cursor = p * COUNT_PER_PAGE
            try:
                j = fetch_tikhub(cid, cursor)
            except (subprocess.SubprocessError, json.JSONDecodeError, RuntimeError, ValueError) as e:
                print(f"  [{tag}] page {p+1} error: {e}", flush=True)
                break
            req_total += 1
            data = j.get("data") or {}
            batch = data.get("videos") or data.get("aweme_list") or []
            if not batch:
                break
            pages += 1
            fresh = [map_video(v, tag) for v in batch if v.get("create_time", 0) >= cutoff]
            fresh_total += len(fresh)
            for it in fresh:
                vid = it["video_id"]
                if not vid:
                    continue
                entry = registry.get(vid) or {
                    "first_seen": now.isoformat(), "topics": [],
                    "created_at": it["created_at"], "author": it["author"],
                    "title": it["title"], "linked": None, "anchor": "",
                }
                entry.update({k: it[k] for k in ("digg", "collect", "comment", "share", "play")})
                entry["created_at"] = it["created_at"]   # tikhub app-API value is authoritative
                if it["linked"] or entry.get("linked") is not True:
                    entry["linked"] = it["linked"]
                    if it["anchor"]:
                        entry["anchor"] = it["anchor"]
                entry["backfill_at"] = now.isoformat()
                if tag not in entry["topics"]:
                    entry["topics"].append(tag)
                registry[vid] = entry
            print(f"  [{tag}] page {p+1}: {len(batch)} items, {len(fresh)} fresh(48h)", flush=True)
            if len(fresh) < STOP_WHEN_FRESH_BELOW:
                print(f"  [{tag}] page {p+1} below threshold — topic done "
                      f"({pages} pages, {fresh_total} fresh)", flush=True)
                break
            time.sleep(SLEEP_BETWEEN_REQ)
        new_total += fresh_total
        time.sleep(SLEEP_BETWEEN_REQ)

    for vid in list(registry):
        if registry[vid].get("created_at", 0) < cutoff:
            del registry[vid]

    os.makedirs("data/snapshots", exist_ok=True)
    items_all = []
    for vid, e in registry.items():
        items_all.append({
            "video_id": vid, "author": e.get("author", ""), "title": e.get("title", ""),
            "digg": e.get("digg", 0), "collect": e.get("collect", 0),
            "comment": e.get("comment", 0), "share": e.get("share", 0),
            "play": e.get("play", 0), "created_at": e.get("created_at", 0),
            "linked": e.get("linked"), "anchor": e.get("anchor", ""),
            "topics": e.get("topics", []),
        })
    items_all.sort(key=lambda x: x["created_at"], reverse=True)
    snap = {"captured_at": now.isoformat().replace("+00:00", "Z"), "window_hours": MAX_AGE_HOURS,
            "products": PRODUCTS, "source": "tikhub-backfill", "topics": {}}
    for tag in CHALLENGES:
        tag_items = [i for i in items_all if tag in i["topics"]]
        snap["topics"][tag] = {"api_status": "success" if req_total else "error",
                               "count_fresh": len(tag_items), "items": tag_items}

    # snapshot (only if this half-hour bucket has no snapshot yet — avoid clobbering primary)
    ts = now.replace(second=0, microsecond=0)
    ts = ts.replace(minute=0 if ts.minute < 30 else 30)
    tss = ts.strftime("%Y-%m-%dT%H%M") + "Z"
    snap_path = f"data/snapshots/{tss}.json"
    if not os.path.exists(snap_path + ".enc") and not os.path.exists(snap_path):
        store_json(snap_path, snap, DK)
        idx_path = "data/index.json"
        idx = read_json(idx_path, DK, [])
        if f"snapshots/{tss}.json" not in idx:
            idx.insert(0, f"snapshots/{tss}.json")
        store_json(idx_path, idx[:1440], DK)
    # latest.json + registry always updated (backfill data is additive and fresh)
    store_json("data/latest.json", snap, DK)
    store_json(reg_path, registry, DK)

    linked_total = sum(1 for e in registry.values() if e.get("linked") is True)
    print(f"\nBACKFILL: requests={req_total} | new_fresh={new_total} | "
          f"registry_size={len(registry)} | linked={linked_total}")
    return 0 if req_total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
