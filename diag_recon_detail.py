"""Детальный разбор mismatch: сторона расхождения, тайминг, связь с book/delta.

Только чтение data/pm.duckdb. Окно прогона 04.08 (ts >= 1785831115420).

Запуск: python diag_recon_detail.py
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

import duckdb

RUN_START = 1785831115420


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/pm.duckdb")
    args = p.parse_args(argv)
    con = duckdb.connect(str(Path(args.db)), read_only=True)
    try:
        mm = con.execute(
            "SELECT ts_recv_ms, token_id, seq, n_levels_ours, n_levels_theirs, "
            "max_abs_diff_price, max_abs_diff_size "
            "FROM recon_checks WHERE verdict='mismatch' AND ts_recv_ms>=? "
            "ORDER BY token_id, ts_recv_ms",
            [RUN_START],
        ).fetchall()
        # все чеки окна для устойчивости
        all_checks = con.execute(
            "SELECT ts_recv_ms, token_id, seq, verdict FROM recon_checks "
            "WHERE ts_recv_ms>=? ORDER BY token_id, ts_recv_ms",
            [RUN_START],
        ).fetchall()
        # book_snapshots окна: для каждого токена времена book-событий и seq
        snaps = con.execute(
            "SELECT ts_recv_ms, token_id, seq, source FROM book_snapshots "
            "WHERE ts_recv_ms>=? ORDER BY token_id, ts_recv_ms",
            [RUN_START],
        ).fetchall()
        markets = dict(
            con.execute(
                "SELECT token_id, COALESCE(market_id, event_id, token_id) "
                "FROM markets_tracked"
            ).fetchall()
        )
    finally:
        con.close()

    print(f"mismatch в окне: {len(mm)}")
    # совместное распределение (price, size, leveldiff)
    joint = Counter()
    for r in mm:
        joint[(r[5], r[6], r[3] - r[4])] += 1
    print("\n(price_diff, size_diff, dlevels) -> кол-во:")
    for k in sorted(joint, key=lambda k: -joint[k])[:25]:
        print("   ", k, joint[k])

    # токены: какие рынки, сколько mismatch, длительность окна
    tok_mm = Counter(r[1] for r in mm)
    print("\n== токены с mismatch в окне ==")
    for tok, n in sorted(tok_mm.items(), key=lambda kv: -kv[1]):
        mk = markets.get(tok, "?")
        t_first = min(r[0] for r in mm if r[1] == tok)
        t_last = max(r[0] for r in mm if r[1] == tok)
        print(f"  {n:>3}  {tok[:24]}... market={mk[:28]} {t_first}..{t_last}")

    # тайминг: распределение по 5-минуткам
    buckets = Counter()
    for r in mm:
        bucket = (r[0] - RUN_START) // (5 * 60_000)
        buckets[bucket] += 1
    print("\nmismatch по 5-минуткам от старта:", dict(sorted(buckets.items())))

    # устойчивость: следующий чек токена после mismatch
    by_tok: dict[str, list[tuple[int, str]]] = {}
    for r in all_checks:
        by_tok.setdefault(r[1], []).append((r[0], r[3]))
    nxt: Counter = Counter()
    for tok, checks in by_tok.items():
        for i, (ts, v) in enumerate(checks):
            if v == "mismatch" and i + 1 < len(checks):
                nxt[checks[i + 1][1]] += 1
            elif v == "mismatch":
                nxt["NULL"] += 1
    print("\nследующий чек после mismatch:", dict(nxt))

    # временной разрыв перед mismatch (последний чек до него того же токена)
    gaps: list[int] = []
    for tok, checks in by_tok.items():
        for i, (ts, v) in enumerate(checks):
            if v == "mismatch":
                prev_ts = checks[i - 1][0] if i > 0 else None
                gaps.append((None if prev_ts is None else ts - prev_ts) // 1000)
    from collections import Counter as C2
    print("\nсекунды от предыдущего чека до mismatch (распределение):",
          dict(C2(gaps).most_common(15)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
