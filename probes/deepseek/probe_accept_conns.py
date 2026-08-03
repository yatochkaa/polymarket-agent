"""ЗАДАЧА 2 (после ответа владельца): приёмка мультисоединённого транспорта.

Два прогона ПОДРЯД на ОДНОМ фиксированном наборе рынков, крипта, по 10 минут:
  прогон 1 — эталон, одно соединение (n_conns=1);
  прогон 2 — авто-число соединений (n_conns=0, по числу рынков).
Оба прогона используют один и тот же фиксированный набор токенов
(fixed_tokens/fixed_slugs), поэтому сравнение не страдает от ротации
крипто-рынков раз в 5 минут: набор не меняется между прогонами.

Отчёт по каждому прогону — четыре числа сверок:
  всего сверок, mismatch всего, из них самозалечившихся, из них устойчивых.
Отдельно: gap_intervals (по reason) и переподключения.

Критерии приёмки (все — именованные константы, порог не подкручивается):
  C1   ноль переподключений за прогон (reconnects == 0);
  C2   ноль УСТОЙЧИВЫХ mismatch; если самозалечившихся больше 5% от всех
       сверок — СТОП (гонка стала массовой);
  C2-бис  ноль gap_intervals с reason в {server_resync, disconnect,
       process_restart} (time_gap — ротация/тишина, не транспортная потеря);
  C3   доля mismatch прогона 2 (многосоединённый) не выше 1.5 x доли
       прогона 1 (эталон); выше — ПРОВАЛ, назвать причину, не подкручивать.

Определение самозалечивания (формулировка владельца):
  mismatch по токену X в момент t самозалечился, если СЛЕДУЮЩАЯ же сверка
  по тому же токену X — match, и между ними не было ни REST-подкачки
  (book_snapshots source='rest_backfill'), ни ресинка
  (gap reason='server_resync'), ни переподключения (gap reason='disconnect').
  В противном случае mismatch устойчивый.

Выход: 0 при всех PASS, 1 при ПРОВАЛЕ, 2 при СТОП-условии.
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, r"C:\Users\awf\Desktop\test")

import src.collect.store as store
import src.collect.ws_collector as wsc

# --- Именованные пороги (критерии приёмки; не подкручивать) ----------------
FAIL_MULTIPLIER = 1.5  # C3: прогон 2 не выше 1.5 x эталон по доле mismatch
SELF_HEAL_LIMIT = 0.05  # C2: самозалечившиеся <= 5% от всех сверок
RECONNECT_LIMIT = 0  # C1: ноль переподключений
RUN_MINUTES = 10.0  # длительность каждого прогона
VERTICAL = "crypto"

DB_BASELINE = Path("data/accept_1conn.duckdb")
DB_MULTI = Path("data/accept_multiconn.duckdb")
EXPORT_BASELINE = Path("data/accept_1conn_export")
EXPORT_MULTI = Path("data/accept_multiconn_export")

# reason'ы, которые НЕ являются транспортной потерей (ротация/тишина)
NON_LOSS_GAP_REASONS = frozenset({"time_gap"})
FAIL_GAP_REASONS = frozenset({"server_resync", "disconnect", "process_restart"})


def _fresh_db(path: Path) -> None:
    """Удаляет старую БД прогона: каждый прогон стартует с чистой БД."""
    path.unlink(missing_ok=True)


def _classify_recons(db_path: Path) -> dict[str, int]:
    """Считает сверки и классифицирует mismatch: самозалечившийся / устойчивый.

    Порядок сверок — (token_id, seq): seq монотонен по токену, поэтому
    «следующая же сверка по тому же токену» = следующая строка в этом порядке.
    """
    con = store.connect(db_path)
    try:
        rows = con.execute(
            "SELECT ts_recv_ms, token_id, seq, verdict "
            "FROM recon_checks ORDER BY token_id, seq"
        ).fetchall()

        gaps: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
        for token_id, start_ms, end_ms, reason in con.execute(
            "SELECT token_id, start_ms, end_ms, reason "
            "FROM gap_intervals WHERE reason IN ('server_resync', 'disconnect')"
        ).fetchall():
            gaps[token_id].append((start_ms, end_ms, reason))

        backfills: dict[str, list[int]] = defaultdict(list)
        for token_id, ts in con.execute(
            "SELECT DISTINCT token_id, ts_recv_ms FROM book_snapshots "
            "WHERE source='rest_backfill'"
        ).fetchall():
            backfills[token_id].append(ts)
        for v in backfills.values():
            v.sort()
    finally:
        con.close()

    by_token: dict[str, list[tuple[int, int, str]]] = defaultdict(list)
    for ts_recv_ms, token_id, _seq, verdict in rows:
        by_token[token_id].append((ts_recv_ms, verdict == "mismatch"))

    total = 0
    mismatch_total = 0
    self_healed = 0
    for token_id, checks in by_token.items():
        g = gaps.get(token_id, [])
        bf = backfills.get(token_id, [])
        bf_idx = 0
        for i, (ts_i, is_mismatch) in enumerate(checks):
            total += 1
            if not is_mismatch:
                continue
            mismatch_total += 1
            if i + 1 >= len(checks):
                continue  # нет следующей сверки -> устойчивый
            ts_next, next_matched = checks[i + 1]
            if not next_matched:
                continue  # следующая сверка не match -> устойчивый

            healed = True
            if bf_idx < len(bf):
                while bf_idx < len(bf) and bf[bf_idx] <= ts_i:
                    bf_idx += 1
                if bf_idx < len(bf) and bf[bf_idx] <= ts_next:
                    healed = False  # между сверками был REST-бэкфилл
            for start_ms, end_ms, _reason in g:
                if start_ms < ts_next and end_ms > ts_i:
                    healed = False  # ресинк/отвал между сверками
            if healed:
                self_healed += 1

    return {
        "total": total,
        "mismatch": mismatch_total,
        "self_healed": self_healed,
        "persistent": mismatch_total - self_healed,
    }


def _count_gaps_and_reconnects(db_path: Path) -> dict[str, int]:
    """gap_intervals по reason и число переподключений.

    Переподключение = отдельный эпизод (start_ms, end_ms) среди строк
    reason='disconnect' (record_disconnect пишет одну строку на токен).
    """
    con = store.connect(db_path)
    try:
        by_reason: dict[str, int] = defaultdict(int)
        episodes = set()
        for token_id, start_ms, end_ms, reason in con.execute(
            "SELECT token_id, start_ms, end_ms, reason FROM gap_intervals"
        ).fetchall():
            by_reason[reason] += 1
            if reason == "disconnect":
                episodes.add((start_ms, end_ms))
    finally:
        con.close()
    return {
        "gap_total": sum(by_reason.values()),
        "gap_by_reason": dict(by_reason),
        "reconnects": len(episodes),
    }


def _verdict_c2(self_healed: int, total: int, persistent: int) -> str:
    if persistent > 0:
        return "FAIL"
    if total > 0 and self_healed / total > SELF_HEAL_LIMIT:
        return "STOP"
    return "PASS"


async def _run_once(
    *,
    db_path: Path,
    export_root: Path,
    n_conns: int,
    tokens: list[str],
    slugs: dict[str, str],
) -> int:
    _fresh_db(db_path)
    return await wsc.run(
        minutes=RUN_MINUTES,
        db_path=db_path,
        export_root=export_root,
        drop_rate=0.0,
        n_conns=n_conns,
        vertical=VERTICAL,
        fixed_tokens=tokens,
        fixed_slugs=slugs,
    )


async def main() -> int:
    # Единственный discovery в начале: оба прогона используют этот набор.
    tokens, slugs = await asyncio.to_thread(wsc._discover_with_client, VERTICAL)
    print(f"[probe] набор рынков: токенов={len(tokens)}", flush=True)

    print("--- прогон 1 (эталон, одно соединение) ---", flush=True)
    code1 = await _run_once(
        db_path=DB_BASELINE,
        export_root=EXPORT_BASELINE,
        n_conns=1,
        tokens=tokens,
        slugs=slugs,
    )
    rec1 = _classify_recons(DB_BASELINE)
    gap1 = _count_gaps_and_reconnects(DB_BASELINE)
    print(f"[probe] прогон 1 exit_code={code1}", flush=True)

    print("--- прогон 2 (авто-число соединений) ---", flush=True)
    code2 = await _run_once(
        db_path=DB_MULTI,
        export_root=EXPORT_MULTI,
        n_conns=0,
        tokens=tokens,
        slugs=slugs,
    )
    rec2 = _classify_recons(DB_MULTI)
    gap2 = _count_gaps_and_reconnects(DB_MULTI)
    print(f"[probe] прогон 2 exit_code={code2}", flush=True)

    share1 = rec1["mismatch"] / rec1["total"] if rec1["total"] else 0.0
    share2 = rec2["mismatch"] / rec2["total"] if rec2["total"] else 0.0

    v1 = {
        "C1": "PASS" if gap1["reconnects"] <= RECONNECT_LIMIT else "FAIL",
        "C2": _verdict_c2(rec1["self_healed"], rec1["total"], rec1["persistent"]),
        "C2_bis": "PASS"
        if not (FAIL_GAP_REASONS & set(gap1["gap_by_reason"]))
        else "FAIL",
        "C3": "PASS",  # C3 сравнивает прогоны; на эталонном прогоне не считается
    }
    v2 = {
        "C1": "PASS" if gap2["reconnects"] <= RECONNECT_LIMIT else "FAIL",
        "C2": _verdict_c2(rec2["self_healed"], rec2["total"], rec2["persistent"]),
        "C2_bis": "PASS"
        if not (FAIL_GAP_REASONS & set(gap2["gap_by_reason"]))
        else "FAIL",
        "C3": "PASS" if share2 <= FAIL_MULTIPLIER * share1 else "FAIL",
    }

    print("\n================ ОТЧЁТ ПРИЁМКИ ================", flush=True)
    for tag, rec, gap, v in (
        ("ПРОГОН 1 (эталон, 1 соединение)", rec1, gap1, v1),
        ("ПРОГОН 2 (многосоединённый)", rec2, gap2, v2),
    ):
        print(
            f"{tag}:\n"
            f"  сверок всего:   {rec['total']}\n"
            f"  mismatch всего: {rec['mismatch']}\n"
            f"  самозалечившихся: {rec['self_healed']}\n"
            f"  устойчивых:     {rec['persistent']}\n"
            f"  gap_intervals:  {gap['gap_by_reason']}\n"
            f"  переподключений: {gap['reconnects']}\n"
            f"  вердикты:       {v}",
            flush=True,
        )
    print(
        f"доля mismatch эталон={share1:.4f}; многосоединённый={share2:.4f}; "
        f"порог C3={FAIL_MULTIPLIER * share1:.4f}",
        flush=True,
    )

    all_pass = all(val == "PASS" for val in v2.values())
    if not all_pass:
        print("ПРОВАЛ: нарушен критерий приёмки (см. вердикты прогона 2).", flush=True)
        return 1
    if v1["C2"] == "STOP" or v2["C2"] == "STOP":
        print("СТОП: самозалечившихся больше 5% от всех сверок (гонка массовая).", flush=True)
        return 2
    print("ПРИЁМКА ПРОЙДЕНА.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
