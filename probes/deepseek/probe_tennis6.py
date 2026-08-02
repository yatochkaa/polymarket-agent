"""Определить окно discovery для тенниса: связь slug_date/endDate, live-маркеты."""
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"
DATE_END = re.compile(r"\d{4}-\d{2}-\d{2}$")
DOUBLES = "doubles"

def slug_date(slug: str) -> str | None:
    m = DATE_END.search(slug)
    return m.group(0) if m else None

def main() -> None:
    now = datetime.now(timezone.utc)
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        # просканируем несколько endDate-окон и соберём матчевые события
        windows = [
            ("now-3d..now+1d", now - timedelta(days=3), now + timedelta(days=1)),
            ("now-3d..now+8d", now - timedelta(days=3), now + timedelta(days=8)),
            ("now-1d..now+1d", now - timedelta(days=1), now + timedelta(days=1)),
        ]
        all_matches: dict[str, dict] = {}
        for label, lo, hi in windows:
            params = {
                "tag_slug": "tennis", "closed": "false", "limit": 100, "offset": 0,
                "end_date_min": lo.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "end_date_max": hi.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            r = c.get(f"{GAMMA}/events", params=params)
            data = r.json() if r.status_code == 200 else []
            # считаем только события-матчи (дата в конце слага, не doubles)
            matches = []
            for ev in data:
                slug = ev.get("slug") or ""
                sd = slug_date(slug)
                if not sd or DOUBLES in slug:
                    continue
                # матчевый winner-рынок: slug рынка == slug события
                winner = None
                for m in (ev.get("markets") or []):
                    if (m.get("slug") or "") == slug:
                        winner = m
                        break
                if winner is None:
                    continue
                matches.append((slug, sd, ev.get("endDate"), winner.get("acceptingOrders")))
            print(f"[{label}] status={r.status_code} events={len(data)} matches={len(matches)}")
            for slug, sd, ed, ao in matches[:10]:
                off = ""
                if isinstance(ed, str):
                    try:
                        d_ed = datetime.fromisoformat(ed.replace("Z", "+00:00"))
                        d_sd = datetime.fromisoformat(sd).replace(tzinfo=timezone.utc)
                        off = f" endDate-slugDate={(d_ed - d_sd).days}d"
                    except ValueError:
                        pass
                print(f"    {slug} | slug_date={sd} | endDate={ed} | accepting={ao}{off}")
            # гистограмма acceptingOrders
            ao_cnt = Counter(str(m[3]) for m in matches)
            print(f"    accepting histogram: {dict(ao_cnt)}")
            for slug, sd, ed, ao in matches:
                all_matches[slug] = (sd, ed, ao)

        print(f"\nУникальных матчевых событий во всех окнах: {len(all_matches)}")
        # по сегодняшней дате
        today = now.strftime("%Y-%m-%d")
        todays = [s for s, (sd, ed, ao) in all_matches.items() if sd == today]
        print(f"  матчей с slug_date=сегодня({today}): {len(todays)}")
        for s in todays[:20]:
            print("   ", s, all_matches[s])

if __name__ == "__main__":
    main()
