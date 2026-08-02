"""ЗАДАЧА 3: сырые WS-сообщения для одного токена, один коннект.

Цель: увидеть паттерн book_array/price_change реплея сервера.
Подписываемся на один токен, пишем типы сообщений и первые price_change."""
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

    msg_types: dict[str, int] = {}
    book_count = 0
    delta_count = 0
    n_messages = 0
    t0 = time.monotonic()
    async with websockets.connect(
        WS_URL,
        ping_interval=20.0,
        max_size=2**22,
        user_agent_header=USER_AGENT,
    ) as ws:
        await ws.send(json.dumps({"type": "market", "assets_ids": [token]}))
        print("подписан", flush=True)
        while time.monotonic() - t0 < 30:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            n_messages += 1
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            if isinstance(payload, list):
                msg_types["list"] = msg_types.get("list", 0) + 1
                for ev in payload:
                    t = ev.get("event_type") or ev.get("type")
                    if t == "price_change" and delta_count < 8:
                        delta_count += 1
                        print("delta:", str(ev.get("asset_id"))[:12], ev.get("ts"),
                              ev.get("price"), ev.get("size"), ev.get("side"),
                              ev.get("best_bid"), ev.get("best_ask"), flush=True)
                continue
            t = payload.get("event_type") or payload.get("type")
            msg_types[t] = msg_types.get(t, 0) + 1
            if t == "book":
                book_count += 1
            if t == "price_change":
                delta_count += 1
                if delta_count <= 8:
                    print("delta:", payload.get("asset_id","")[:12], payload.get("ts"),
                          payload.get("price"), payload.get("size"), payload.get("side"),
                          payload.get("best_bid"), payload.get("best_ask"), flush=True)
    print("n_messages:", n_messages, flush=True)
    print("types:", msg_types, flush=True)
    print("book:", book_count, "price_change:", delta_count, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
