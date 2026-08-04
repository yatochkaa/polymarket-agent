"""Суточная сводка теннисного сбора.

Читает data/tennis.duckdb (по умолчанию), по каждой сессии и соединению
печатает: messages / recons / mismatch / max_silence_s / reconnects,
отдельно recon_checks (всего, warmup/match/mismatch), объём БД в МБ,
прогноз на сутки по темпу роста с момента старта сбора.

Порог тревоги: прогноз > 5 ГБ/сутки -> STOP (останавливаемся и думаем).
Именованная константа ALARM_GB_PER_DAY.

Запуск (за человеком):
    python probes/deepseek/tennis_daily_summary.py

Код возврата: 0 — норма, 2 — тревога (прогноз за порогом), 1 — ошибка
(нет базы/нет данных/нечего показывать).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import duckdb

from src.collect import store
from src.collect.schema import CONN_STATS_COLUMNS, RECON_CHECKS_COLUMNS

# Порог тревоги: свыше этой скорости роста останавливаемся и думаем.
ALARM_GB_PER_DAY = 5.0
# Минимальная длительность сбора для осмысленного прогноза (иначе — честно
# "мало данных", а не выдуманная цифра).
MIN_ELAPSED_S_FOR_FORECAST = 60.0


def _rows(con, table: str, order_by: str) -> list[dict]:
    cols = [d[0] for d in con.execute(f"SELECT * FROM {table} LIMIT 0").description]
    rows = con.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
    return [dict(zip(cols, r)) for r in rows]


def _sum_int(rows: list[dict], col: str) -> int:
    return int(sum(r[col] or 0 for r in rows))


def _fmt_elapsed(ms: int) -> str:
    s = max(0, int(ms) // 1000)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}ч {m:02d}м {sec:02d}с"


def build_report(db_path: Path) -> tuple[dict, int]:
    """Считает сводку. Возвращает (report, exit_code)."""
    if not db_path.exists():
        return {"error": f"базы нет: {db_path}"}, 1

    size_bytes = db_path.stat().st_size
    report: dict = {"db_path": str(db_path), "size_mb": size_bytes / (1024 * 1024)}
    try:
        con = store.connect(db_path)
    except duckdb.duckdb.IOException as exc:
        # Факт 2026-08-03 (duckdb 1.1.3): второй процесс на том же файле
        # не подключается, включая read_only. Поэтому сводка запускается
        # ТОЛЬКО после остановки коллектора (см. SCHEMAS.md).
        report["error"] = (
            "БД заблокирована работающим коллектором "
            f"(duckdb.duckdb.IOException: {exc}). Сводка запускается только "
            "после остановки коллектора."
        )
        return report, 1
    try:
        sessions = _rows(con, "collector_sessions", "started_ms")
        conn_stats = _rows(con, "conn_stats", "session_id, conn_id")
        recon_checks = _rows(con, "recon_checks", "ts_recv_ms")
    finally:
        con.close()

    if not sessions:
        report["error"] = "collector_sessions пуст: сбор ещё не стартовал."
        return report, 1

    first_started = min(s["started_ms"] for s in sessions)
    last_ended = max((s["ended_ms"] or 0) for s in sessions)
    now_ms = int(os.environ.get("TENNIS_SUMMARY_NOW_MS", 0)) or None
    if now_ms is None:
        # Прокси-часы: максимум из окон сообщений/сессий и текущего времени
        # недоступны намеренно — берём реальные часы.
        now_ms = int(__import__("time").time() * 1000)
    elapsed_ms = now_ms - first_started
    report["sessions"] = len(sessions)
    report["window"] = {
        "first_started_ms": first_started,
        "last_ended_ms": last_ended or None,
        "elapsed_s": max(0, elapsed_ms // 1000),
    }

    by_session: dict[str, dict] = {}
    for cs in conn_stats:
        sid = cs["session_id"]
        by_session.setdefault(sid, []).append(cs)
    report["conn_stats_by_session"] = {
        sid: sorted(rows, key=lambda r: int(r["conn_id"])) for sid, rows in by_session.items()
    }

    if not conn_stats:
        report["error"] = "conn_stats пуст: ни одна сессия не дошла до записи статистики."
        return report, 1

    report["recon_checks"] = {
        "total": len(recon_checks),
        "mismatch": sum(1 for r in recon_checks if r["verdict"] == "mismatch"),
        "match": sum(1 for r in recon_checks if r["verdict"] == "match"),
        "warmup": sum(1 for r in recon_checks if r["verdict"] == "warmup"),
    }

    # Прогноз: скорость роста БД от старта сбора до сейчас.
    forecast_gb = None
    if elapsed_ms >= int(MIN_ELAPSED_S_FOR_FORECAST * 1000):
        forecast_gb = size_bytes / (elapsed_ms / 1000.0) * 86400.0 / (1024**3)
    report["forecast_gb_per_day"] = forecast_gb
    report["alarm"] = bool(forecast_gb is not None and forecast_gb > ALARM_GB_PER_DAY)
    return report, (2 if report["alarm"] else 0)


def print_report(report: dict) -> None:
    if report.get("error"):
        print(f"НЕТ ДАННЫХ: {report['error']}", flush=True)
        return
    w = report["window"]
    print(
        f"=== СУТОЧНАЯ СВОДКА TENNIS ===",
        flush=True,
    )
    print(
        f"база: {report['db_path']}  размер: {report['size_mb']:.1f} МБ",
        flush=True,
    )
    print(
        f"сессий: {report['sessions']}  окно: с {w['first_started_ms']} "
        f"по {w['last_ended_ms'] or 'сейчас'} ({_fmt_elapsed(w['elapsed_s'] * 1000)})",
        flush=True,
    )
    print(
        "по соединениям: conn | токенов | messages | recons | mismatch | "
        "reconnects | max_silence_s",
        flush=True,
    )
    for sid, rows in report["conn_stats_by_session"].items():
        print(f"  сессия {sid}:", flush=True)
        for c in rows:
            print(
                f"    conn {int(c['conn_id']) + 1:>2} | "
                f"{int(c['n_tokens']):>6} | {int(c['messages']):>10} | "
                f"{int(c['recons']):>7} | {int(c['recons_mismatch']):>6} | "
                f"{int(c['reconnects']):>8} | {c['max_silence_s']:>9.1f}",
                flush=True,
            )
    rc = report["recon_checks"]
    print(
        f"recon_checks: всего {rc['total']} (match {rc['match']}, "
        f"mismatch {rc['mismatch']}, warmup {rc['warmup']})",
        flush=True,
    )
    fg = report["forecast_gb_per_day"]
    if fg is None:
        print(
            f"прогноз на сутки: мало данных (< {MIN_ELAPSED_S_FOR_FORECAST:.0f} с сбора)",
            flush=True,
        )
    else:
        print(f"прогноз на сутки: {fg:.2f} ГБ/сутки", flush=True)
        if report["alarm"]:
            print(
                f"ТРЕВОГА: прогноз {fg:.2f} > {ALARM_GB_PER_DAY:.0f} ГБ/сутки — "
                f"STOP, останавливаемся и думаем.",
                flush=True,
            )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=str(store.DEFAULT_DB_PATH), help="путь к duckdb")
    args = p.parse_args(argv)
    report, code = build_report(Path(args.db))
    print_report(report)
    return code


if __name__ == "__main__":
    sys.exit(main())
