"""Точный реплей потока tick_changes одного токена -> сравнение recon_checks.

Воспроизводит LiveBook коллектора по tick_changes.raw ВСЕГО прогона и для
каждого book-события пересчитывает recon_check, сверяя с сохранёнными
строками recon_checks (только чтение data/pm.duckdb). Расхождение реплея
с базой указывает, какие сообщения коллектор НЕ применил (или применил
иначе), чем реплей.

Запуск: python diag_recon_replay2.py <token_id>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

from src.collect.recon import LiveBook, recon_check
from src.collect.ws_collector import BookEvent, DeltaEvent, interpret_message


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("token")
    p.add_argument("--db", default="data/pm.duckdb")
    p.add_argument("--since-ms", type=int, default=1785831115420)
    args = p.parse_args(argv)

    con = duckdb.connect(str(Path(args.db)), read_only=True)
    try:
        ticks = con.execute(
            "SELECT ts_recv_ms, seq, raw FROM tick_changes "
            "WHERE token_id=? AND ts_recv_ms>=? ORDER BY ts_recv_ms, seq",
            [args.token, args.since_ms],
        ).fetchall()
        recons = {
            r[0]: r for r in con.execute(
                "SELECT ts_recv_ms, n_levels_ours, n_levels_theirs, "
                "max_abs_diff_price, max_abs_diff_size, verdict "
                "FROM recon_checks WHERE token_id=? AND ts_recv_ms>=?",
                [args.token, args.since_ms],
            ).fetchall()
        }
    finally:
        con.close()

    print(f"токен {args.token[:24]}... tick-строк: {len(ticks)}, recon-строк: {len(recons)}")
    book = LiveBook()
    n_book = 0
    n_agree = 0
    n_mismatch_hits = 0
    for ts, seq, raw in ticks:
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        events, _ = interpret_message(payload, ts)
        tag = payload.get("event_type")
        for ev in events:
            if tag == "book" and isinstance(ev, BookEvent):
                theirs_bids = {p: s for p, s in ev.bids}
                theirs_asks = {p: s for p, s in ev.asks}
                rc = recon_check(
                    ts_recv_ms=ts, token_id=args.token, seq=seq,
                    ours=book, theirs_bids=theirs_bids, theirs_asks=theirs_asks,
                )
                n_book += 1
                db = recons.get(ts)
                agree = (db is not None
                         and db[1] == rc["n_levels_ours"]
                         and db[2] == rc["n_levels_theirs"]
                         and db[3] == rc["max_abs_diff_price"]
                         and db[4] == rc["max_abs_diff_size"]
                         and db[5] == rc["verdict"])
                if agree:
                    n_agree += 1
                else:
                    flag = "DB-recon" if db is not None else "нет в DB"
                    print(f"ts={ts} РАСХОЖДЕНИЕ реплей vs {flag}: "
                          f"реплей=ours{rc['n_levels_ours']}/theirs{rc['n_levels_theirs']}"
                          f"/p{rc['max_abs_diff_price']}/s{rc['max_abs_diff_size']}"
                          f"/{rc['verdict']}  db={db if db is not None else '-'}")
                if rc["verdict"] == "mismatch":
                    n_mismatch_hits += 1
                    if db and db[5] == "mismatch":
                        print(f"    [совпал mismatch; db seq уточни]")
                book.set_book(ev.bids, ev.asks)
            elif tag == "price_change" and isinstance(ev, DeltaEvent):
                book.apply_change(ev.side, ev.price, ev.size)
    print(f"\nbook-событий: {n_book}, согласие реплея с DB: {n_agree}/{n_book}, "
          f"mismatch по реплею: {n_mismatch_hits}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
