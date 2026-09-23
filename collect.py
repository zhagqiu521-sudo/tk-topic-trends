#!/usr/bin/env python3
"""Tk topic trends collector — hourly snapshot of hashtag video stats via tikwm free API.

Rate-limit aware: tikwm allows 1 req/sec per IP; we sleep between all requests.
Output: data/snapshots/<UTC timestamp>.json + data/latest.json (also printed to stdout).
"""
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone, timedelta

API = "https://www.tikwm.com/api/challenge/posts"
# challenge_id values resolved & verified 2026-09-23 (see delivery doc section 8)
CHALLENGES = {
    "capcut": "1663935709411330",
    "capcutpioneer": "7356025154310733831",
    "capcutnow": "1684704995991554",
}
PAGES_PER_TOPIC = 2       # 2 pages x 20 items on first pass; tune later
COUNT_PER_PAGE = 20
MAX_AGE_DAYS = 7
SLEEP_BETWEEN_REQ = 1.6   # tikwm: 1 request/sec per IP

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# tikwm's Cloudflare blocks python-urllib's TLS fingerprint (verified 2026-09-23);
# curl with browser headers passes from GitHub Actions egress — always shell out.
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


def collect_topic(tag, cid, cutoff):
    """Fetch pages until older-than-cutoff items dominate or pages exhausted."""
    videos, cursor, pages, api_status = [], 0, 0, None
    for _ in range(PAGES_PER_TOPIC):
        url = f"{API}/?challenge_id={cid}&count={COUNT_PER_PAGE}&cursor={cursor}"
        try:
            j = fetch_json(url)
        except (subprocess.SubprocessError, json.JSONDecodeError, ValueError) as e:
            api_status = f"error: {e}"
            break
        api_status = j.get("msg", "?")
        if j.get("code") != 0 or not j.get("data"):
            break
        batch = j["data"].get("videos") or []
        fresh = [v for v in batch if v.get("create_time", 0) >= cutoff]
        videos.extend(fresh)
        pages += 1
        has_more = j["data"].get("hasMore")
        oldest = min((v.get("create_time", 0) for v in batch), default=0)
        print(f"  [{tag}] page {pages}: {len(batch)} items, {len(fresh)} fresh(7d), "
              f"oldest={datetime.fromtimestamp(oldest, tz=timezone.utc):%Y-%m-%d} hasMore={has_more}", flush=True)
        if not has_more or oldest >= cutoff and not fresh and batch:
            break
        cursor = j["data"].get("cursor") or (cursor + COUNT_PER_PAGE)
        time.sleep(SLEEP_BETWEEN_REQ)

    items = []
    for v in videos:
        items.append({
            "video_id": str(v.get("video_id", "")),
            "author": (v.get("author") or {}).get("unique_id", ""),
            "title": (v.get("title") or "")[:120],
            "digg": v.get("digg_count", 0),          # likes
            "collect": v.get("collect_count", 0),    # favorites
            "comment": v.get("comment_count", 0),
            "share": v.get("share_count", 0),
            "play": v.get("play_count", 0),
            "created_at": v.get("create_time", 0),   # unix sec
        })
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return {"api_status": api_status, "pages": pages, "count_7d": len(items), "items": items}


def main():
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    cutoff = int((now - timedelta(days=MAX_AGE_DAYS)).timestamp())
    snap = {"captured_at": now.isoformat(), "window_days": MAX_AGE_DAYS, "topics": {}}

    for tag, cid in CHALLENGES.items():
        print(f"[{tag}] collecting...", flush=True)
        snap["topics"][tag] = collect_topic(tag, cid, cutoff)
        time.sleep(SLEEP_BETWEEN_REQ)

    ok = all(t["api_status"] == "success" for t in snap["topics"].values())
    total = sum(t["count_7d"] for t in snap["topics"].values())
    print(f"\nVERDICT: {'PASS' if ok and total > 0 else 'FAIL'} | api_ok={ok} | fresh_items_7d={total}")
    for tag, t in snap["topics"].items():
        top = t["items"][0] if t["items"] else None
        top_s = (f"top: {top['digg']} likes/{top['collect']} favs {top['title'][:40]}"
                 if top else "no fresh items")
        print(f"  {tag}: {t['api_status']} | {t['count_7d']} items | {top_s}")

    ts = now.strftime("%Y-%m-%dT%H%M") + "Z"
    os.makedirs("data/snapshots", exist_ok=True)
    with open(f"data/snapshots/{ts}.json", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    with open("data/latest.json", "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=1)
    return 0 if (ok and total > 0) else 1


if __name__ == "__main__":
    sys.exit(main())
