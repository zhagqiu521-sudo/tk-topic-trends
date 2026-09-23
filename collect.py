#!/usr/bin/env python3
"""Tk topic trends collector v4 — TikHub source (per-account billing, no IP lottery).

v4 changes vs v3 (2026-09-24):
- Data source: TikHub app-v3 hashtag list (ch_id + region), $0.001/req, 10 req/s limit.
  Replaces tikwm (per-IP daily quota exhausted by shared egress pools).
- Anchor detection: anchors[] with keyword comes IN THE LIST RESPONSE — per-video
  page checks and flags.json are retired.
- createTime comes from TikTok app API directly (authoritative) — SSR calibration retired.
- Registry / 48h rolling window / half-hour dedupe / snapshot format: unchanged.

Cost model: 6 topics × PAGES(2) × 48 rounds/day = 576 req/day ≈ $0.58/day.
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
PAGES_PER_TOPIC = 2       # 40 items per topic per round; page1-2 ≈ all within 48h (measured)
REGION = "US"
COUNT_PER_PAGE = 20
MAX_AGE_HOURS = 48
SLEEP_BETWEEN_REQ = 0.4   # TikHub allows 10/s; stay modest

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def fetch_tikhub(ch_id, cursor):
    token = os.environ.get("TIKHUB_TOKEN", "")
    if not token:
        raise RuntimeError("TIKHUB_TOKEN not set")
    url = (f"{API}?ch_id={ch_id}&count={COUNT_PER_PAGE}&cursor={cursor}&region={REGION}")
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


def collect_topic(tag, cid, cutoff):
    """Fetch PAGES_PER_TOPIC pages; keep in-window items. Returns (items, pages, status)."""
    items, pages, status = [], 0, "success"
    for p in range(PAGES_PER_TOPIC):
        cursor = p * COUNT_PER_PAGE
        try:
            j = fetch_tikhub(cid, cursor)
        except (subprocess.SubprocessError, json.JSONDecodeError, RuntimeError, ValueError) as e:
            status = f"error: {e}"
            break
        data = j.get("data") or {}
        batch = data.get("videos") or data.get("aweme_list") or []
        if not batch:
            status = j.get("msg", "empty")
            break
        fresh = [map_video(v, tag) for v in batch if v.get("create_time", 0) >= cutoff]
        items.extend(fresh)
        pages += 1
        print(f"  [{tag}] page {p+1}: {len(batch)} items, {len(fresh)} fresh(48h)", flush=True)
        time.sleep(SLEEP_BETWEEN_REQ)
    return items, pages, status


def load_json_file(path, default):
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return default
    return default


def main():
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    now = now.replace(minute=0 if now.minute < 30 else 30)
    ts = now.strftime("%Y-%m-%dT%H%M") + "Z"
    if os.path.exists(os.path.join("data", "snapshots", ts + ".json")):
        print(f"snapshot for {ts} already exists — skipping (half-hour dedupe)")
        return 0

    cutoff = int((now - timedelta(hours=MAX_AGE_HOURS)).timestamp())
    reg_path = "data/registry.json"
    registry = load_json_file(reg_path, {})

    snap = {"captured_at": now.isoformat(), "window_hours": MAX_AGE_HOURS,
            "products": PRODUCTS, "source": "tikhub", "topics": {}}

    statuses = {}
    for tag, cid in CHALLENGES.items():
        print(f"[{tag}] collecting (tikhub)...", flush=True)
        items, pages, status = collect_topic(tag, cid, cutoff)
        statuses[tag] = status
        print(f"  [{tag}] fresh scanned this run: {len(items)}", flush=True)
        for it in items:
            vid = it["video_id"]
            if not vid:
                continue
            entry = registry.get(vid) or {
                "first_seen": now.isoformat(),
                "topics": [],
                "created_at": it["created_at"],
                "author": it["author"],
                "title": it["title"],
                "linked": None,
                "anchor": "",
            }
            entry.update({k: it[k] for k in ("digg", "collect", "comment", "share", "play")})
            entry["created_at"] = it["created_at"]      # tikhub app-API value is authoritative
            entry["linked"] = it["linked"]
            entry["anchor"] = it["anchor"]
            entry["last_seen"] = now.isoformat()
            if tag not in entry["topics"]:
                entry["topics"].append(tag)
            registry[vid] = entry
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
    for tag in CHALLENGES:
        tag_items = [i for i in items_all if tag in i["topics"]]
        snap["topics"][tag] = {"api_status": statuses.get(tag, "success"),
                               "count_fresh": len(tag_items), "items": tag_items}

    with open(f"data/snapshots/{ts}.json", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    with open(reg_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False)

    idx_path = "data/index.json"
    idx = load_json_file(idx_path, [])
    if f"snapshots/{ts}.json" not in idx:
        idx.insert(0, f"snapshots/{ts}.json")
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx[:1440], f, indent=1)

    linked_total = sum(1 for e in registry.values() if e.get("linked") is True)
    total = len(items_all)
    ok = all(s == "success" for s in statuses.values())
    print(f"\nVERDICT: {'PASS' if ok and total > 0 else 'FAIL'} | registry_size={total} | linked={linked_total}")
    return 0 if (ok and total > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
