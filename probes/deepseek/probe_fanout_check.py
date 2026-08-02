"""ЗАДАЧА 3: проверка fan-out — шлёт ли сервер события чужих токенов
на соединение, которое их НЕ подписывало.

Открываем 2 соединения. Соед.A подписывается на токены [X], соед.B на
токены [Y] (X != Y, из разных рынков). Считаем: сколько событий для Y
пришло на соед.A (должно быть 0, если fan-out нет; >0 если сервер
шлёт всё на каждое соединение)."""
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
    tokens = [o.token_id for o in res.outcomes]
    # X из рынка 1, Y из рынка 2 (разные slug)
    x = tokens[0]
    y = tokens[1]
    print("X:", x[:16], " Y:", y[:16], flush=True)

    counts = {"on_A_for_Y": 0, "on_A_for_X": 0, "on_B_for_X": 0, "on_B_for_Y": 0}
    t0 = time.monotonic()

    async def conn_a() -> None:
        async with websockets.connect(
            WS_URL, ping_interval=20.0, max_size=2**22, user_agent_header=USER_AGENT,
        ) as ws:
            await ws.send(json.dumps({"type": "market", "assets_ids": [x]}))
            while time.monotonic() - t0 < 20:
                raw = await asyncio.wait_for(ws.recv(), timeout=25)
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(payload, list):
                    continue
                pc = payload.get("price_changes") or []
                for ev in pc:
                    aid = str(ev.get("asset_id"))
                    if aid == y:
                        counts["on_A_for_Y"] += 1
                    if aid == x:
                        counts["on_A_for_X"] += 1

    async def conn_b() -> None:
        async with websockets.connect(
            WS_URL, ping_interval=20.0, max_size=2**22, user_agent_header=USER_AGENT,
        ) as ws:
            await ws.send(json.dumps({"type": "market", "assets_ids": [y]}))
            while time.monotonic() - t0 < 20:
                raw = await asyncio.wait_for(ws.recv(), timeout=25)
                try:
                    payload = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(payload, list):
                    continue
                pc = payload.get("price_changes") or []
                for ev in pc:
                    aid = str(ev.get("asset_id"))
                    if aid == x:
                        counts["on_B_for_X"] += 1
                    if aid == y:
                        counts["on_B_for_Y"] += 1

    await asyncio.gather(conn_a(), conn_b())
    print("counts:", counts, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
