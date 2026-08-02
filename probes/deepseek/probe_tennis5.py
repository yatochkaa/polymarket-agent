"""Структура всех рынков одного матча: как отличить winner-market."""
from datetime import datetime, timezone

import httpx

GAMMA = "https://gamma-api.polymarket.com"

def main() -> None:
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        r = c.get(f"{GAMMA}/events", params={"slug": "atp-sonego-grieksp-2026-08-03"})
        ev = r.json()[0]
        print("event:", ev.get("slug"), "endDate:", ev.get("endDate"))
        for m in ev.get("markets") or []:
            print(f"  {m.get('slug')}")
            print(f"    q={m.get('question')!r}")
            print(f"    outcomes={m.get('outcomes')} n_outcomes={len(m.get('outcomes') or [])}")
            print(f"    accepting={m.get('acceptingOrders')} closed={m.get('closed')} endDate={m.get('endDate')}")
            print(f"    clob={m.get('clobTokenIds')}")

if __name__ == "__main__":
    main()
