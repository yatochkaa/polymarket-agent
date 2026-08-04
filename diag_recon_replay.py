"""Восстановление потока сообщений токена из tick_changes.raw и сверка.

По данным data/pm.duckdb (только чтение). Показывает сообщения (book /
price_change / last_trade_price) вокруг заданного времени mismatch и
состояние, которое из них собирается LiveBook.

Запуск: python diag_recon_replay.py <token_id> <ts_ms> [--window-s 60]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import duckdb

from src.collect.recon import LiveBook
from src.collect.ws_collector import BookEvent, DeltaEvent, interpret_message


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("token")
    p.add_argument("ts_ms", type=int)
    p.add_argument("--db", default="data/pm.duckdb")
    p.add_argument("--window-s", type=float, default=60.0)
    args = p.parse_args(argv)

    con = duckdb.connect(str(Path(args.db)), read_only=True)
    try:
        rows = con.execute(
            "SELECT ts_recv_ms, raw FROM tick_changes "
            "WHERE token_id=? AND ts_recv_ms BETWEEN ? AND ? "
            "ORDER BY ts_recv_ms, seq",
            [args.token, args.ts_ms - int(args.window_s * 1000),
             args.ts_ms + int(args.window_s * 1000)],
        ).fetchall()
    finally:
        con.close()

    print(f"токен {args.token[:24]}... сообщений в окне: {len(rows)}")
    book = LiveBook()
    for ts, raw in rows:
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        events, _ = interpret_message(payload, ts)
        tag = payload.get("event_type")
        for ev in events:
            if tag == "book" and isinstance(ev, BookEvent):
                book.set_book(ev.bids, ev.asks)
                print(f"{ts}  BOOK  bids={len(ev.bids)} asks={len(ev.asks)} "
                      f"bb={book.best_bid()} ba={book.best_ask()} "
                      f"levels={book.n_levels}")
            elif tag == "price_change" and isinstance(ev, DeltaEvent):
                book.apply_change(ev.side, ev.price, ev.size)
                print(f"{ts}  PC {ev.side} p={ev.price} s={ev.size} "
                      f"levels={book.n_levels}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
