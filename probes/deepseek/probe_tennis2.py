"""Прощупать /events?tag_slug=tennis: окна шире, структура событий."""
import json
from datetime import datetime, timedelta, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"

def fetch(c, lo, hi, offset=0):
    r = c.get(f"{GAMMA}/events", params={
        "tag_slug": "tennis", "closed": "false",
        "limit": 100, "offset": offset,
        "end_date_min": lo, "end_date_max": hi,
    })
    return r.status_code, r.json()

def main() -> None:
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        # 1) несуществующий тег — проверка, что tag_slug фильтрует
        st, data = fetch(c,
            (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        print("nosuchtag window now±1h: status", st, "events", len(data))

        for label, lo_h, hi_h in [("now±12h", 12, 12), ("now-1d..now+12h", 24, 12),
                                  ("now-2d..now+2d", 48, 48)]:
            st, data = fetch(c,
                (now - timedelta(hours=lo_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                (now + timedelta(hours=hi_h)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            print(f"{label}: status {st} events {len(data)}")
            if data:
                ev = data[0]
                print("  first event slug:", ev.get("slug"), "closed:", ev.get("closed"), "endDate:", ev.get("endDate"))
                markets = ev.get("markets") or []
                print("  markets:", len(markets))
                for m in markets[:4]:
                    print("    - slug:", m.get("slug"), "| q:", (m.get("question") or "")[:50])
                    print("      outcomes:", m.get("outcomes"), "| clob:", m.get("clobTokenIds"))
                    print("      accepting:", m.get("acceptingOrders"))
                # посмотрим все уникальные слаги событий
                slugs = [e.get("slug") for e in data]
                print("  event slugs sample:", slugs[:20])
                break

if __name__ == "__main__":
    main()
