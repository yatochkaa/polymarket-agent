import asyncio, json, sys, time
sys.path.insert(0, r"C:\Users\awf\Desktop\test")
import httpx, websockets
from src.collect.ws_collector import WS_URL, USER_AGENT
from src.validate.discovery import updown_outcomes

async def main():
    with httpx.Client(base_url="https://gamma-api.polymarket.com", timeout=30.0) as gc:
        res = updown_outcomes(gc)
    tokens = [o.token_id for o in res.outcomes]
    x = tokens[0]
    print("tokenov v otkrytii:", len(tokens))
    print("podpiska tolko na X:", x[:24], flush=True)
    seen = {}
    t0 = time.monotonic()
    async with websockets.connect(WS_URL, ping_interval=20.0,
                                  max_size=2**22,
                                  user_agent_header=USER_AGENT) as ws:
        await ws.send(json.dumps({"type": "market", "assets_ids": [x]}))
        while time.monotonic() - t0 < 30:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                break
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            items = payload if isinstance(payload, list) else [payload]
            for msg in items:
                if not isinstance(msg, dict):
                    continue
                for ev in (msg.get("price_changes") or []):
                    a = str(ev.get("asset_id"))
                    seen[a] = seen.get(a, 0) + 1
                a2 = msg.get("asset_id")
                if a2:
                    seen[str(a2)] = seen.get(str(a2), 0) + 1
    print("raznyh asset_id polucheno:", len(seen))
    print("X sredi nih:", x in seen)
    for a, n in sorted(seen.items(), key=lambda kv: -kv[1])[:10]:
        print("  ", a[:24], n, "<-- X" if a == x else "")
    print("VERDIKT:", "FAN-OUT" if len(seen) > 2 else "net fan-out")

if __name__ == "__main__":
    asyncio.run(main())
