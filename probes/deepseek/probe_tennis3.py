"""Прощупать /events?tag_slug=tennis&closed=false без дат: сколько событий, endDate."""
from datetime import datetime, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"

def main() -> None:
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.get(f"{GAMMA}/events", params={
            "tag_slug": "tennis", "closed": "false", "limit": 100, "offset": 0,
        })
        print("status", r.status_code)
        data = r.json()
        print("events:", len(data))
        ends = []
        for ev in data:
            ends.append(ev.get("endDate"))
            markets = ev.get("markets") or []
            match_markets = [m for m in markets if not (m.get("slug") or "").endswith("-completed-match")]
            print(f"  {ev.get('slug')} closed={ev.get('closed')} endDate={ev.get('endDate')} "
                  f"match_markets={len(match_markets)} accepting={[m.get('acceptingOrders') for m in match_markets]}")
        print("\nendDate range:", min(ends) if ends else None, "..", max(ends) if ends else None)

if __name__ == "__main__":
    main()
