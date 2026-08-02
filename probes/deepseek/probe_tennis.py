"""Прощупать /events?tag_slug=tennis: структура события и рынков матча."""
import json
import sys
from datetime import datetime, timedelta, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"

def main() -> None:
    now = datetime.now(timezone.utc)
    lo = (now - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi = (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.get(f"{GAMMA}/events", params={
            "tag_slug": "tennis", "closed": "false",
            "limit": 100, "offset": 0,
            "end_date_min": lo, "end_date_max": hi,
        })
        print("status", r.status_code)
        data = r.json()
        print("events:", len(data))
        for ev in data[:3]:
            print("=== EVENT ===")
            print("  slug:", ev.get("slug"))
            print("  title:", ev.get("title"))
            print("  closed:", ev.get("closed"))
            print("  endDate:", ev.get("endDate"))
            print("  startDate:", ev.get("startDate"))
            print("  keys:", sorted(ev.keys())[:40])
            markets = ev.get("markets")
            print("  markets:", len(markets) if isinstance(markets, list) else markets)
            for m in (markets or [])[:6]:
                print("    - slug:", m.get("slug"), "| question:", (m.get("question") or "")[:60])
                print("      outcomes:", m.get("outcomes"), "| clobTokenIds:", m.get("clobTokenIds"))
                print("      acceptingOrders:", m.get("acceptingOrders"), "| closed:", m.get("closed"))
                print("      keys:", sorted(m.keys())[:30])

if __name__ == "__main__":
    main()
