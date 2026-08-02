"""ЗАДАЧА 3: структура raw сообщений сервера (один токен)."""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"C:\Users\awf\Desktop\test")

import httpx
import websockets

from src.collect.ws_collector import WS_URL, USER_AGENT
from src.validate.discovery import updown_outcomes

GAMMA_URL = "https://gamma-api.polymarket.com"


async def main() -> None:
    with httpx.Client(base_url=GAMMA_URL, timeout=30.0) as gc:
        res = updown_outcomes(gc)
    token = res.outcomes[0].token_id
    print("токен:", token, flush=True)
    shown = 0
    t0 = time.monotonic()
    async with websockets.connect(
        WS_URL, ping_interval=20.0, max_size=2**22, user_agent_header=USER_AGENT,
    ) as ws:
        await ws.send(json.dumps({"type": "market", "assets_ids": [token]}))
        print("подписан", flush=True)
        while time.monotonic() - t0 < 25 and shown < 4:
            raw = await asyncio.wait_for(ws.recv(), timeout=25)
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if isinstance(payload, list):
                print(f"\n=== LIST из {len(payload)} элементов; показано 2 ===", flush=True)
                for ev in payload[:2]:
                    print(json.dumps(ev)[:500], flush=True)
                shown += 1
            else:
                print("\n=== ОБЪЕКТ type=", payload.get("event_type") or payload.get("type"),
                      "===", flush=True)
                print(json.dumps(payload)[:500], flush=True)
                shown += 1


if __name__ == "__main__":
    asyncio.run(main())
