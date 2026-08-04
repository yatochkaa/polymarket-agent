"""Диагностика recon_checks: рынки токенов с mismatch, парность ног.

Только чтение data/pm.duckdb. Шаг 1 задачи 2026-08-04: каким рынкам
принадлежат токены с mismatch, каждый ли рынок представлен обеими ногами.

Запуск: python diag_recon_mismatch.py [--db data/pm.duckdb] [--since-ms 0]
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import duckdb


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="data/pm.duckdb")
    p.add_argument("--since-ms", type=int, default=0,
                   help="нижняя граница ts_recv_ms (по умолчанию всё)")
    args = p.parse_args(argv)

    con = duckdb.connect(str(Path(args.db)), read_only=True)
    try:
        rows = con.execute(
            "SELECT ts_recv_ms, token_id, seq, n_levels_ours, n_levels_theirs, "
            "max_abs_diff_price, max_abs_diff_size, verdict FROM recon_checks "
            "WHERE verdict = 'mismatch' AND ts_recv_ms >= ? ORDER BY token_id, ts_recv_ms",
            [args.since_ms],
        ).fetchall()
        markets = dict(
            con.execute(
                "SELECT token_id, COALESCE(market_id, event_id, token_id) "
                "FROM markets_tracked"
            ).fetchall()
        )
        sessions = con.execute(
            "SELECT session_id, started_ms, ended_ms, git_commit, exit_reason "
            "FROM collector_sessions ORDER BY started_ms DESC LIMIT 10"
        ).fetchall()
    finally:
        con.close()

    print("== последние сессии ==")
    for s in sessions:
        print("  ", s)

    mm = [r for r in rows]
    print(f"\n== mismatch всего: {len(mm)} (по окну) ==")

    # парность по рынкам
    by_market: dict[str, list[str]] = {}
    for r in mm:
        tok = r[1]
        mk = markets.get(tok, "<unknown>")
        by_market.setdefault(mk, []).append(tok)

    print(f"рынков с mismatch: {len(by_market)}")
    one_leg = [mk for mk, toks in by_market.items() if len(set(toks)) == 1]
    two_leg = [mk for mk, toks in by_market.items() if len(set(toks)) >= 2]
    print(f"рынков с ОДНОЙ ногой: {len(one_leg)}")
    print(f"рынков с ДВУМЯ ногами: {len(two_leg)}")
    if one_leg:
        print("  примеры одной ноги:")
        for mk in one_leg[:10]:
            print("   ", mk, set(by_market[mk]))

    # разбивки
    from collections import Counter
    price_diffs = Counter(round(r[5], 8) for r in mm)
    size_diffs = Counter(round(r[6], 8) for r in mm)
    level_diffs = Counter(r[3] - r[4] for r in mm)
    print("\nprice diff распределение:", dict(sorted(price_diffs.items())))
    print("size diff распределение:", dict(sorted(size_diffs.items())))
    print("n_levels_ours - n_levels_theirs:", dict(sorted(level_diffs.items())))

    # токены по количеству mismatch
    tok_counts = Counter(r[1] for r in mm)
    print(f"\nтокенов с mismatch: {len(tok_counts)}")
    print("топ:", sorted(tok_counts.items(), key=lambda kv: -kv[1])[:15])

    # устойчивость: следующий чек того же токена после mismatch
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
