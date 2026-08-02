"""ЗАДАЧА 3: классифицировать events_skipped_dedup по типу события и
по содержанию ключа. 2 соединения, crypto, 2 минуты."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\awf\Desktop\test")

from collections import Counter

import src.collect.ws_collector as wsc
from src.collect import ws_collector

SKIP_TYPES: Counter = Counter()
SKIP_SAMPLES: list[str] = []

orig_handle = ws_collector.Collector.handle_event


def patched_handle(self, event, ts_recv_ms):
    if isinstance(event, wsc.BookEvent):
        typ = "book"
        key = f"book|{event.token_id}|{event.ts_server_ms}|{event.raw}"
    elif isinstance(event, wsc.DeltaEvent):
        typ = "delta"
        key = (
            f"delta|{event.token_id}|{event.ts_server_ms}|{event.price}|"
            f"{event.size}|{event.side}|{event.best_bid}|{event.best_ask}"
        )
    else:
        typ = "trade"
        key = (
            f"trade|{event.token_id}|{event.ts_server_ms}|{event.price}|"
            f"{event.size}|{event.side}"
        )
    if self._is_duplicate(key):
        SKIP_TYPES[typ] += 1
        if len(SKIP_SAMPLES) < 5:
            SKIP_SAMPLES.append(f"{typ}: {key}")
        # пропускаем, как в _handle_delta/_handle_trade
        if isinstance(event, wsc.BookEvent):
            self.stats["events_skipped_dedup"] += 1
        return
    # применить исходную логику, но не дать ей повторно вызвать dedup
    self._dedup_in_progress = True
    try:
        orig_handle(self, event, ts_recv_ms)
    finally:
        self._dedup_in_progress = False


async def main() -> None:
    db = Path("data/probe_dedup_classify.duckdb")
    if db.exists():
        db.unlink()
    exit_code = await wsc.run(
        minutes=2.0,
        db_path=db,
        export_root=Path("data/probe_dedup_classify_export"),
        drop_rate=0.0,
        n_conns=2,
        vertical="crypto",
    )
    print(f"exit_code={exit_code}", flush=True)
    print("SKIP_TYPES:", dict(SKIP_TYPES), flush=True)
    for s in SKIP_SAMPLES:
        print("sample:", s[:200], flush=True)


if __name__ == "__main__":
    ws_collector.Collector.handle_event = patched_handle
    asyncio.run(main())
