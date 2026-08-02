"""Проверка: как отобрать живые/текущие матчи по end_date окну."""
from datetime import datetime, timedelta, timezone
import re

import httpx

GAMMA = "https://gamma-api.polymarket.com"

DATE_END = re.compile(r"\d{4}-\d{2}-\d{2}$")

def main() -> None:
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        # окно endDate вокруг now±2 дня
        lo = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = c.get(f"{GAMMA}/events", params={
            "tag_slug": "tennis", "closed": "false",
            "limit": 100, "offset": 0,
            "end_date_min": lo, "end_date_max": hi,
        })
        data = r.json()
        print("window endDate now±2d: events", len(data))
        for ev in data:
            slug = ev.get("slug") or ""
            is_match = bool(DATE_END.search(slug)) and "doubles" not in slug
            print(f"  match={is_match} {slug} start={ev.get('startDate')} end={ev.get('endDate')}")

        # проверим поле startDate на будущих матчах (atp-sonego 08-03)
        r2 = c.get(f"{GAMMA}/events", params={
            "slug": "atp-sonego-grieksp-2026-08-03",
        })
        if r2.status_code == 200:
            ev = r2.json()
            if isinstance(ev, list) and ev:
                ev = ev[0]
                print("\natp-sonego-grieksp-2026-08-03:")
                print("  startDate:", ev.get("startDate"))
                print("  endDate:", ev.get("endDate"))
                print("  closed:", ev.get("closed"))

if __name__ == "__main__":
    main()
