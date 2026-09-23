#!/usr/bin/env python3
"""Tk topic trends collector — half-hourly deep scan of hashtag videos via tikwm free API.

Design (v2, 2026-09-24):
- Two topics only: capcut (1663935709411330), hypic (1667855826908166)
- Rotating scan windows: each run scans 25 pages starting from a rotating offset
  (8 windows × 25 pages = 200 pages deep per topic across 4 hours) — multiplies
  coverage without extra per-run quota.
- 48h rolling registry (data/registry.json): every scanned item is upserted by
  video_id, so works never vanish between runs; entries age out after 48h.
- Anchor (linked) detection: first-seen videos get ONE page fetch (SSR carries
  "anchors"), cached forever in the registry; also calibrates createTime against
  TikTok's own SSR value.
- tikwm free limits: 1 req/sec per IP, 10k/day per IP. This config: ~50 req/run
  + up to 40 page checks (tiktok.com, free of tikwm quota).
"""
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

API = "https://www.tikwm.com/api/challenge/posts"
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

PAGES_PER_TOPIC = 50      # pages scanned per topic per run (from rotating offset)
ROTATION_STEPS = 8        # 8 windows × 50 pages = 400 pages deep per topic across 4 hours
COUNT_PER_PAGE = 20
MAX_AGE_HOURS = 48        # rolling registry window
SLEEP_BETWEEN_REQ = 1.6   # tikwm: 1 request/sec per IP
MAX_PAGE_CHECKS = 40      # anchor page-checks per run (first-seen videos only)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
ANCHOR_RE = re.compile(r'"anchors":\[\{"id":"\d+","type":(\d+),"keyword":"([^"]*)"')
CT_RE = re.compile(r'"createTime":(\d+)')


def fetch_json(url):
    out = subprocess.run(
        ["curl", "-s", "-m", "30", "--compressed",
         "-H", f"User-Agent: {UA}",
         "-H", "Accept: application/json, text/plain, */*",
         "-H", "Accept-Language: en-US,en;q=0.9",
         "-H", "Referer: https://www.tiktok.com/",
         url],
        capture_output=True, text=True, timeout=45, check=True)
    return json.loads(out.stdout)


def check_anchor(vid, author):
    """One page fetch: authoritative createTime + anchors (SSR). None on failure."""
    url = f"https://www.tiktok.com/@{author}/video/{vid}"
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "20", "-L",
             "-H", f"User-Agent: {UA}",
             "-H", "Accept: text/html,application/xhtml+xml",
             "-H", "Accept-Language: en-US,en;q=0.9",
             url],
            capture_output=True, text=True, timeout=30, check=True)
        s = out.stdout
        res = {"linked": False, "keyword": "", "ct": None}
        idx = s.find('"webapp.video-detail"')
        sub = s[idx:idx + 300000] if idx >= 0 else s
        m = ANCHOR_RE.search(sub)
        if m:
            res["linked"] = True
            res["keyword"] = m.group(2)
        c = CT_RE.search(sub)
        if c:
            res["ct"] = int(c.group(1))
        return res
    except subprocess.SubprocessError:
        return None


def collect_topic(tag, cid, cutoff, rot_start_page):
    """Hybrid scan: always refresh freshest pages 0-4 (stats every 30 min for the
    newest cohort) + a rotating deep window of PAGES_PER_TOPIC pages (coverage)."""
    page_nos = list(range(0, 5)) + list(range(rot_start_page, rot_start_page + PAGES_PER_TOPIC))
    videos, pages, status = [], 0, "success"
    seen = set()
    for pno in page_nos:
        if pno in seen:
            continue
        seen.add(pno)
        cursor = pno * COUNT_PER_PAGE
        url = f"{API}/?challenge_id={cid}&count={COUNT_PER_PAGE}&cursor={cursor}"
        try:
            j = fetch_json(url)
        except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as e:
            status = f"error: {e}"
            break
        if j.get("code") != 0 or not j.get("data"):
            status = j.get("msg", "unknown")
            break
        batch = j["data"].get("videos") or []
        fresh = [v for v in batch if v.get("create_time", 0) >= cutoff]
        videos.extend(fresh)
        pages += 1
        if not j["data"].get("hasMore"):
            break
        cursor += COUNT_PER_PAGE
        time.sleep(SLEEP_BETWEEN_REQ)
    return videos, pages, status


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
    bucket_idx = int(now.timestamp() // 1800) % ROTATION_STEPS
    start_page = bucket_idx * PAGES_PER_TOPIC

    reg_path = "data/registry.json"
    registry = load_json_file(reg_path, {})

    snap = {"captured_at": now.isoformat(), "window_hours": MAX_AGE_HOURS,
            "products": PRODUCTS, "rotation": {"start_page": start_page, "steps": ROTATION_STEPS},
            "topics": {}}

    statuses = {}
    for tag, cid in CHALLENGES.items():
        print(f"[{tag}] collecting (fresh 0-4 + deep window {start_page}-{start_page + PAGES_PER_TOPIC})...", flush=True)
        videos, pages, status = collect_topic(tag, cid, cutoff, start_page)
        statuses[tag] = status
        print(f"  [{tag}] fresh scanned this run: {len(videos)}", flush=True)
        for v in videos:
            vid = str(v.get("video_id", ""))
            if not vid:
                continue
            entry = registry.get(vid) or {
                "first_seen": now.isoformat(),
                "topics": [],
                "linked": None,
                "anchor": "",
                "created_at": v.get("create_time", 0),
                "author": (v.get("author") or {}).get("unique_id", ""),
                "title": (v.get("title") or "")[:120],
            }
            entry.update({
                "digg": v.get("digg_count", 0),
                "collect": v.get("collect_count", 0),
                "comment": v.get("comment_count", 0),
                "share": v.get("share_count", 0),
                "play": v.get("play_count", 0),
                "last_seen": now.isoformat(),
            })
            if tag not in entry["topics"]:
                entry["topics"].append(tag)
            registry[vid] = entry
        time.sleep(SLEEP_BETWEEN_REQ)

    # prune entries older than the 48h window
    for vid in list(registry):
        if registry[vid].get("created_at", 0) < cutoff:
            del registry[vid]

    # anchor page-checks for first-seen videos (+ createTime calibration)
    to_check = [vid for vid, e in registry.items() if e.get("linked") is None]
    checked = 0
    for vid in to_check[:MAX_PAGE_CHECKS]:
        res = check_anchor(vid, registry[vid].get("author", ""))
        checked += 1
        if res is None:
            continue  # failed: stays unchecked, retried next run
        registry[vid]["linked"] = res["linked"]
        registry[vid]["anchor"] = res.get("keyword", "")
        if res.get("ct"):
            registry[vid]["created_at"] = res["ct"]  # calibrate against TikTok SSR
            registry[vid]["ct_verified"] = True
        time.sleep(2)
    linked_total = sum(1 for e in registry.values() if e.get("linked") is True)
    print(f"anchor checks: {checked} checked this run | linked total in registry: {linked_total}", flush=True)

    os.makedirs("data/snapshots", exist_ok=True)

    # build per-topic item views from the registry
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
        json.dump(idx[:1440], f, indent=1)  # 30 days of half-hourly snapshots

    total = len(items_all)
    ok = all(s == "success" for s in statuses.values())
    print(f"\nVERDICT: {'PASS' if ok and total > 0 else 'FAIL'} | registry_size={total} | "
          f"linked={linked_total} | rotation_window={start_page}-{start_page + PAGES_PER_TOPIC}")
    return 0 if (ok and total > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
