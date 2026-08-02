"""Проверить, фильтрует ли /events по endDate для тенниса и не утекают ли события вне окна."""
import re
from datetime import datetime, timedelta, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATE_END = re.compile(r"(\d{4}-\d{2}-\d{2})$")

def main() -> None:
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        lo = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        hi = (now + timedelta(days=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {"tag_slug": "tennis", "closed": "false", "limit": 100, "offset": 0,
                  "end_date_min": lo, "end_date_max": hi}
        r = c.get(f"{GAMMA}/events", params=params)
        data = r.json()
        print("status", r.status_code, "events", len(data))
        outside = 0
        none_ed = 0
        for ev in data:
            ed = ev.get("endDate")
            if ed is None:
                none_ed += 1
                print("  None endDate:", ev.get("slug"))
                continue
            if not (lo <= ed <= hi):
                outside += 1
                print("  OUTSIDE window:", ev.get("slug"), "endDate=", ed)
        print("none_endDate:", none_ed, "outside_window:", outside)
        # страница 2 — сколько всего в окне
        r2 = c.get(f"{GAMMA}/events", params={**params, "offset": 100})
        page2 = r2.json()
        print("page2 events:", len(page2))
        # верхняя граница окна: какие slug_date ловятся
        slug_dates = set()
        for ev in data + page2:
            m = DATE_END.search(ev.get("slug") or "")
            if m and "doubles" not in (ev.get("slug") or ""):
                slug_dates.add(m.group(1))
        print("slug_dates:", sorted(slug_dates))

if __name__ == "__main__":
    main()
