"""Идеальное окно: endDate [now+5d, now+10d] -> ловит slug_date сегодня/завтра, отсекает старое на сервере."""
import re
from datetime import datetime, timedelta, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATE_END = re.compile(r"(\d{4}-\d{2}-\d{2})$")

def main() -> None:
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        lo = (now + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (now + timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"tag_slug": "tennis", "closed": "false", "limit": 100, "offset": 0,
                  "end_date_min": lo, "end_date_max": hi}
        all_events = []
        offset = 0
        while True:
            r = c.get(f"{GAMMA}/events", params={**params, "offset": offset})
            if r.status_code != 200:
                print("status", r.status_code); break
            page = r.json()
            all_events.extend(page)
            if len(page) < 100:
                break
            offset += 100
            if offset >= 2000:
                print("offset cap"); break
        print("total events:", len(all_events))
        today = now.strftime("%Y-%m-%d")
        counts: dict[str, int] = {}
        for ev in all_events:
            slug = ev.get("slug") or ""
            m = DATE_END.search(slug)
            if not m or "doubles" in slug:
                continue
            winners = [m2 for m2 in (ev.get("markets") or []) if (m2.get("slug") or "") == slug]
            if not winners:
                continue
            ao = winners[0].get("acceptingOrders")
            counts[m.group(1)] = counts.get(m.group(1), 0) + 1
            if m.group(1) in (today, (datetime.fromisoformat(today) - timedelta(days=1)).strftime("%Y-%m-%d")):
                print(f"  {slug} | {m.group(1)} | accepting={ao}")
        print("by slug_date:", {k: counts[k] for k in sorted(counts)})
        total = sum(counts.values())
        print("total winner matches:", total, "tokens:", total * 2)

if __name__ == "__main__":
    main()
