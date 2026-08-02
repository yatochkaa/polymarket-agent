"""Окончательный discovery-тест для тенниса: диапазон slug_date и число матчей."""
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATE_END = re.compile(r"(\d{4}-\d{2}-\d{2})$")
DOUBLES = "doubles"

def slug_date(slug: str) -> str | None:
    m = DATE_END.search(slug)
    return m.group(1) if m else None

def main() -> None:
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        # endDate окно [now-1d, now+9d] -> ловит slug_date от now-8d до now+2d
        lo = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (now + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        all_events = []
        offset = 0
        while True:
            params = {
                "tag_slug": "tennis", "closed": "false", "limit": 100, "offset": offset,
                "end_date_min": lo, "end_date_max": hi,
            }
            r = c.get(f"{GAMMA}/events", params=params)
            if r.status_code != 200:
                print("status", r.status_code); break
            page = r.json()
            all_events.extend(page)
            if len(page) < 100:
                break
            offset += 100
            if offset >= 2000:
                print("offset cap reached"); break
        print("total events:", len(all_events))
        today = now.strftime("%Y-%m-%d")
        by_date: Counter[str] = Counter()
        winner_missing = 0
        multiple_winners = 0
        for ev in all_events:
            slug = ev.get("slug") or ""
            sd = slug_date(slug)
            if not sd or DOUBLES in slug:
                continue
            # winner market = slug == event slug
            winners = [m for m in (ev.get("markets") or []) if (m.get("slug") or "") == slug]
            if not winners:
                winner_missing += 1
                continue
            if len(winners) > 1:
                multiple_winners += 1
            by_date[sd] += 1
        print("matches by slug_date:")
        for d in sorted(by_date):
            mark = " <-- сегодня" if d == today else ""
            print(f"  {d}: {by_date[d]}{mark}")
        print("winner_missing:", winner_missing, "multiple_winners:", multiple_winners)
        total = sum(by_date.values())
        print("total winner matches:", total, "tokens:", total * 2)
        # токены для диапазона [today-2, today+1]
        want = sum(by_date[d] for d in by_date if today >= d >= (datetime.fromisoformat(today) - timedelta(days=2)).strftime("%Y-%m-%d"))
        want2 = sum(by_date[d] for d in by_date if (datetime.fromisoformat(today) - timedelta(days=2)).strftime("%Y-%m-%d") <= d <= (datetime.fromisoformat(today) + timedelta(days=1)).strftime("%Y-%m-%d"))
        print("matches slug_date in [today-2, today+1]:", want2, "tokens:", want2 * 2)

if __name__ == "__main__":
    main()
