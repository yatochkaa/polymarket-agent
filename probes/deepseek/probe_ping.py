"""Probe: does Polymarket WS respond to client pings? Mirrors collector connect params."""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, r"C:\Users\awf\Desktop\test")

from src.validate.discovery import GAMMA_BASE_URL, updown_outcomes  # noqa: E402

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
USER_AGENT = "python-collector/0.1"


def fresh_tokens(n: int = 3) -> list[str]:
    with httpx.Client(base_url=GAMMA_BASE_URL, timeout=30.0) as client:
        res = updown_outcomes(client)
        return [o.token_id for o in res.outcomes[:n]]


async def try_session(tokens: list[str]) -> None:
    async with websockets.connect(
        WS_URL,
        ping_interval=20.0,
        ping_timeout=20.0,
        open_timeout=30.0,
        user_agent_header=USER_AGENT,
        max_size=2**22,
    ) as ws:
        await ws.send(json.dumps({"type": "market", "assets_ids": tokens}))
        print("subscribed:", [t[:12] for t in tokens], flush=True)

        first = await asyncio.wait_for(ws.recv(), timeout=30)
        print("first msg len:", len(first), flush=True)

        # explicit ping test
        t0 = time.monotonic()
        try:
            pong = await asyncio.wait_for(ws.ping(), timeout=10)
            await asyncio.wait_for(pong, timeout=10)
            print(f"ping->pong OK in {time.monotonic()-t0:.3f}s", flush=True)
        except asyncio.TimeoutError:
            print(f"NO PONG within 10s (t0+{time.monotonic()-t0:.3f}s)", flush=True)
        except websockets.exceptions.ConnectionClosed as exc:
            print(f"closed during ping: {exc!r}", flush=True)

        # idle watch 150s: does the default keepalive close it?
        print("idle 150s watch (default keepalive 20/20)", flush=True)
        end = time.monotonic() + 150
        try:
            while time.monotonic() < end:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=15)
                    print(f"  recv: len={len(msg)}", flush=True)
                except asyncio.TimeoutError:
                    print(f"  quiet 15s (t+{time.monotonic()-end+150:.0f}s)", flush=True)
        except websockets.exceptions.ConnectionClosed as exc:
            print(f"CONNECTION CLOSED during idle: {exc!r}", flush=True)
        print("idle watch done", flush=True)


async def main() -> None:
    tokens = fresh_tokens()
    for attempt in range(3):
        try:
            await try_session(tokens)
            break
        except websockets.exceptions.ConnectionClosed as exc:
            print(f"session {attempt+1} closed early: {exc!r} -- retrying", flush=True)
            await asyncio.sleep(2)


asyncio.run(main())
