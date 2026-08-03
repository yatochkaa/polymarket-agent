"""Проверка гипотезы 28.6%: шардинг ПО ТОКЕНАМ + сервер шлёт на подписку
одним токеном его комплемент.

Гипотеза: сервер на подписку одним токеном шлёт и сам токен, и его комплемент
(подтверждено probe_fanout_fixed.py: 2 разных asset_id). Если оба токена ОДНОГО
рынка X и Y попадают на РАЗНЫЕ соединения, то каждое событие приходит на каЖДОЕ
из двух соединений (по разу на каждое). Отсюда удвоение событий (372821 ~ 2*201571),
двойное применение дельты и уезжающая книга, и потому дедуп расхождение уменьшает.

Метод: один рынок, токены X и Y. Соединение A подписывается только на X,
соединение B только на Y. 30 секунд на каждом соединении считаем, сколько
событий пришло по X и сколько по Y. Печатаем четыре числа.

Ожидание при верной гипотезе: каждое соединение видит И X, И Y примерно поровну
(каждое событие учтено на обоих соединениях -> суммарно каждая дельта дважды).
"""
import asyncio
import json
import sys
import time

sys.path.insert(0, r"C:\Users\awf\Desktop\test")

import httpx
import websockets

from src.collect.ws_collector import WS_URL, USER_AGENT
from src.validate.discovery import GAMMA_BASE_URL, updown_outcomes

DURATION_S = 30


def _count_price_changes(payload: object) -> list[str]:
    """Извлекает asset_id всех price_change внутри одного сообщения."""
    ids: list[str] = []
    items = payload if isinstance(payload, list) else [payload]
    for msg in items:
        if not isinstance(msg, dict):
            continue
        for ev in msg.get("price_changes") or []:
            aid = ev.get("asset_id")
            if aid:
                ids.append(str(aid))
        a2 = msg.get("asset_id")
        if a2:
            ids.append(str(a2))
    return ids


async def _conn(token: str, x: str, y: str) -> dict[str, int]:
    """Одно соединение: подписано на один token, 30 с считает события по X и Y."""
    counts = {"X": 0, "Y": 0}
    t0 = time.monotonic()
    async with websockets.connect(
        WS_URL, ping_interval=20.0, max_size=2**22, user_agent_header=USER_AGENT
    ) as ws:
        # Дублировать копию токена не надо -- подпись только на свой.
        await ws.send(json.dumps({"type": "market", "assets_ids": [token]}))
        while time.monotonic() - t0 < DURATION_S:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=DURATION_S + 5)
            except asyncio.TimeoutError:
                break
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            for aid in _count_price_changes(payload):
                if aid == x:
                    counts["X"] += 1
                if aid == y:
                    counts["Y"] += 1
    return counts


async def main() -> None:
    with httpx.Client(base_url=GAMMA_BASE_URL, timeout=30.0,
                      headers={"User-Agent": USER_AGENT}) as gc:
        res = updown_outcomes(gc)
    if not res.outcomes:
        print("VERDIKT: нет живых up/down рынков в окне")
        return

    # Один рынок: первый встреченный updown-слаг, берём ОБА его токена.
    first_slug = None
    x = None
    y = None
    for o in res.outcomes:
        if first_slug is None:
            first_slug = o.market_slug
        if o.market_slug == first_slug:
            if x is None:
                x = o.token_id
            elif y is None:
                y = o.token_id
    if x is None or y is None or x == y:
        print(f"VERDIKT: отладка -- токены рынка {first_slug!r} не найдены: {x=} {y=}")
        return

    print(f"рынок: {first_slug}")
    print(f"подписка A -> X: {x[:24]}  подписка B -> Y: {y[:24]}")

    a, b = await asyncio.gather(_conn(x, x, y), _conn(y, x, y))
    print(f"A: X={a['X']}  Y={a['Y']}")
    print(f"B: X={b['X']}  Y={b['Y']}")

    # Вердикт: если каждая сторона видит и X, и Y примерно поровну -> каждая
    # дельта учтена дважды (удвоение => гипотеза подтверждена).
    a_sees_y = a["Y"] > 0
    b_sees_x = b["X"] > 0
    if a_sees_y and b_sees_x:
        verdict = "ПОДТВЕРЖДЕНО: оба соединения видят и X, и Y (каждая дельта дважды)"
    elif a_sees_y or b_sees_x:
        verdict = "ЧАСТИЧНО: одно соединение видит чужой токен"
    else:
        verdict = "НЕ подтверждено: каждое соединение видит только свой токен"
    print(f"VERDIKT: {verdict}")


if __name__ == "__main__":
    asyncio.run(main())