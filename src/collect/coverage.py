"""Отчёт покрытия коллектора (этап 1, гейт G3).

Запуск:
    python -m src.collect.coverage --db data/pm.duckdb

Печатает:
- окно наблюдения;
- долю времени в пропусках по каждому рынку (token_id);
- медиану / p90 / максимум book_age_ms по каждому рынку;
- число рынков с долей пропусков менее 5%;
- число строк по каждой таблице, распределение recon_checks по verdict,
  список gap_intervals (причины, интервалы), число переподключений.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import duckdb

from . import schema, store

GAP_FRACTION_OK = 0.05  # доля пропусков менее 5% = рынок «хорошо покрыт»


def _gap_fraction(
    con: duckdb.DuckDBPyConnection,
    token_id: str,
    lo_ms: int,
    hi_ms: int,
) -> float:
    """Доля времени [lo, hi] в gap_intervals для токена (клип по окну)."""
    row = con.execute(
        """
        SELECT COALESCE(SUM(GREATEST(LEAST(end_ms, ?), ?)
                        - GREATEST(LEAST(start_ms, ?), ?)), 0)
        FROM (
            SELECT start_ms, end_ms FROM gap_intervals
            WHERE token_id = ? AND start_ms < ? AND end_ms > ?
        )
        """,
        [hi_ms, lo_ms, hi_ms, lo_ms, token_id, hi_ms, lo_ms],
    ).fetchone()
    gap_ms = int(row[0])
    total_ms = hi_ms - lo_ms
    if total_ms <= 0:
        return 0.0
    return gap_ms / total_ms


def _stats_sql(col: str) -> str:
    return f"MEDIAN({col}), PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY {col}), MAX({col})"


def build_report(db_path: Path) -> dict[str, Any]:
    con = store.connect(db_path)
    counts = store.count_rows(con)

    window = con.execute(
        "SELECT MIN(ts_recv_ms), MAX(ts_recv_ms) FROM book_snapshots"
    ).fetchone()
    lo_ms = int(window[0]) if window[0] is not None else None
    hi_ms = int(window[1]) if window[1] is not None else None

    markets: list[dict[str, Any]] = []
    if lo_ms is not None:
        rows = con.execute(
            "SELECT DISTINCT token_id FROM book_snapshots ORDER BY token_id"
        ).fetchall()
        for (token_id,) in rows:
            med, p90, mx = con.execute(
                f"SELECT {_stats_sql('book_age_ms')} FROM book_snapshots "
                "WHERE token_id = ? AND book_age_ms IS NOT NULL",
                [token_id],
            ).fetchone()
            fraction = _gap_fraction(con, token_id, lo_ms, hi_ms)
            markets.append(
                {
                    "token_id": token_id,
                    "gap_fraction": round(fraction, 5),
                    "book_age_median_ms": int(med) if med is not None else None,
                    "book_age_p90_ms": int(p90) if p90 is not None else None,
                    "book_age_max_ms": int(mx) if mx is not None else None,
                }
            )

    recon_dist = {
        str(r[0]): int(r[1])
        for r in con.execute(
            "SELECT verdict, COUNT(*) FROM recon_checks GROUP BY verdict"
        ).fetchall()
    }
    recon_price = [
        (str(r[0]), float(r[1]))
        for r in con.execute(
            "SELECT token_id, MAX(max_abs_diff_price) FROM recon_checks "
            "WHERE verdict = 'mismatch' GROUP BY token_id ORDER BY 2 DESC"
        ).fetchall()
    ]

    gaps = [
        {
            "token_id": str(r[0]),
            "start_ms": int(r[1]),
            "end_ms": int(r[2]),
            "reason": str(r[3]),
            "n_missing": int(r[4]) if r[4] is not None else None,
            "duration_s": round((int(r[2]) - int(r[1])) / 1000.0, 1),
        }
        for r in con.execute(
            "SELECT token_id, start_ms, end_ms, reason, n_missing "
            "FROM gap_intervals ORDER BY start_ms"
        ).fetchall()
    ]

    n_ok = sum(1 for m in markets if m["gap_fraction"] < GAP_FRACTION_OK)
    n_subscribed_total = con.execute(
        "SELECT SUM(markets_subscribed) FROM collector_sessions"
    ).fetchone()[0]
    n_reconnects = len([g for g in gaps if g["reason"] == "disconnect"])
    return {
        "counts": counts,
        "window_ms": {"lo": lo_ms, "hi": hi_ms},
        "markets": markets,
        "n_markets_gap_lt_5pct": n_ok,
        "recon_verdict_distribution": recon_dist,
        "recon_max_abs_diff_price_by_mismatch": recon_price,
        "gaps": gaps,
        "n_sessions": int(n_subscribed_total) if n_subscribed_total is not None else None,
        "n_reconnects": n_reconnects,
        "gap_fraction_ok_threshold": GAP_FRACTION_OK,
    }


def print_report(report: dict[str, Any]) -> None:
    lo, hi = report["window_ms"]["lo"], report["window_ms"]["hi"]
    print("=== ОТЧЁТ ПОКРЫТИЯ КОЛЛЕКТОРА ===")
    if lo is not None:
        print(f"окно наблюдения (ms): {lo} .. {hi}  ({(hi - lo) / 60000.0:.2f} мин)")
    else:
        print("окно наблюдения: пусто (нет снимков)")
    print("строк по таблицам:")
    for table, n in report["counts"].items():
        print(f"  {table}: {n}")
    print(f"распределение recon_checks по verdict: {report['recon_verdict_distribution']}")
    print(f"  из них mismatch max_abs_diff_price (по токену): {report['recon_max_abs_diff_price_by_mismatch']}")
    print(f"переподключений (gap reason=disconnect): {report['n_reconnects']}")
    print(f"рынков с долей пропусков < {report['gap_fraction_ok_threshold']:.0%}: "
          f"{report['n_markets_gap_lt_5pct']} из {len(report['markets'])}")
    print("по рынкам (token_id | доля пропусков | book_age медиана/p90/max, ms):")
    for m in report["markets"]:
        print(
            f"  {m['token_id'][:16]:<18} gap={m['gap_fraction']:.2%}  "
            f"age={m['book_age_median_ms']}/{m['book_age_p90_ms']}/{m['book_age_max_ms']}"
        )
    print("gap_intervals (token | start | end | reason | n_missing | duration_s):")
    for g in report["gaps"]:
        print(
            f"  {g['token_id'][:16]:<18} {g['start_ms']} .. {g['end_ms']}  "
            f"{g['reason']:<15} n_missing={g['n_missing']} d={g['duration_s']}s"
        )


def main(argv: Any = None) -> int:
    p = argparse.ArgumentParser(description="Отчёт покрытия коллектора")
    p.add_argument("--db", default=str(store.DEFAULT_DB_PATH))
    args = p.parse_args(argv)
    print_report(build_report(Path(args.db)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
