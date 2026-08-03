"""ЗАДАЧА 2 (редакция 3, ПРАВКИ 1-3 владельца): приёмка мультисоединённого
транспорта на per-connection статистике с защитой от истечения рынков.

Четыре прогона ПОДРЯД по RUN_MINUTES минут каждый, в порядке
2conn, 1conn, 2conn, 1conn. Рынки выбираются ЗАНОВО перед каждым прогоном
и ТОЛЬКО из рынков с запасом жизни >= MIN_LIFE_MS до истечения (2 длительности
прогона = 12+ мин при 6-минутном прогоне). Если таких рынков нет — проба ждёт
начала нового цикла и не стартует (ПРАВКА 1).

Статистика собирается ПО КАЖДОМУ СОЕДИНЕНИЮ отдельно (таблица conn_stats,
пишет коллектор в конце run()): messages, events, recons, recons_mismatch,
max_silence_s, n_silence_episodes, n_pings_fired, first_msg_ms/last_msg_ms,
n_tokens.

ВАЛИДНОСТЬ ПРОГОНА — отдельно от критериев (ПРАВКА 2). В начале и в конце
прогона считаются токены, у которых рынок ещё открыт (живые по gamma).
Если открытых к концу меньше VALIDITY_FLOOR (90%) от открытых на старте —
прогон НЕВАЛИДЕН, в критерии C2..C5 не входит вообще, повторяется (не FAIL).

Критерии приёмки (окончательные, все — именованные константы):
  C2   устойчивых рассогласований ноль в каждом прогоне; самозалечившихся
       > SELF_HEAL_LIMIT (5%) — СТОП;
  C3   доля mismatch у 2conn не выше FAIL_MULTIPLIER (1.5) x доля у 1conn,
       на СРЕДНИХ по двум прогонам каждой конфигурации;
  C4   ни одно соединение не молчит дольше SILENCE_FAIL_S (90 с) подряд
       при непустом наборе токенов (max_silence_s > 90 => FAIL);
  C5   число сообщений у 2conn не ниже MESSAGE_FLOOR (0.7) от 1conn
       на средних по двум прогонам каждой конфигурации.

ОТЧЁТ по каждому прогону дополнительно содержит (ПРАВКА 3): число открытых
рынков на старте и на финише, время до истечения ближайшего рынка на старте.

Выход: 0 при всех PASS, 1 при ПРОВАЛЕ, 2 при СТОП-условии C2 (гонка массовая).
"""
from __future__ import annotations

import asyncio
import sys
from collections import defaultdict
from pathlib import Path

import httpx

sys.path.insert(0, r"C:\Users\awf\Desktop\test")

import src.collect.store as store
import src.collect.ws_collector as wsc
from src.validate.discovery import updown_outcomes

# --- Именованные пороги (критерии приёмки; не подкручивать) ----------------
FAIL_MULTIPLIER = 1.5  # C3: 2conn не выше 1.5 x 1conn по доле mismatch
SELF_HEAL_LIMIT = 0.05  # C2: самозалечившиеся <= 5% от всех сверок
SILENCE_FAIL_S = 90.0  # C4: молчание сверх этого = незамеченный разрыв
MESSAGE_FLOOR = 0.7  # C5: 2conn >= 0.7 от 1conn по числу сообщений
RUN_MINUTES = 6.0  # длительность каждого прогона
MIN_LIFE_S = 2 * RUN_MINUTES * 60  # ПРАВКА 1: запас жизни до истечения
MIN_LIFE_MS = int(MIN_LIFE_S * 1000)
SELECT_POLL_S = 30.0  # ПРАВКА 1: период опроса нового цикла
VALIDITY_FLOOR = 0.9  # ПРАВКА 2: открытых к концу >= 90% от старта
VERTICAL = "crypto"

# Порядок прогонов: (имя конфигурации, n_conns; 0 = авто по числу рынков)
RUN_SPEC = [
    ("2conn", 0),
    ("1conn", 1),
    ("2conn", 0),
    ("1conn", 1),
]

DB_PATTERN = "data/accept_run{i}.duckdb"
EXPORT_PATTERN = "data/accept_run{i}_export"


def _fresh_db(path: Path) -> None:
    """Удаляет старую БД прогона: каждый прогон стартует с чистой БД."""
    path.unlink(missing_ok=True)


def _discover_live() -> tuple[list[str], dict[str, str], dict[str, int]]:
    """Живые up/down токены + время истечения (endDate, ms) по каждому.

    Токены без endDate исключаются: рынок без срока жизни не проверить.
    """
    with httpx.Client(base_url=wsc.GAMMA_URL, timeout=30.0) as gc:
        res = updown_outcomes(gc)
    tokens: list[str] = []
    slugs: dict[str, str] = {}
    expiry: dict[str, int] = {}
    for o in res.outcomes:
        if o.end_date is None:
            continue
        tokens.append(o.token_id)
        slugs[o.token_id] = o.market_slug
        expiry[o.token_id] = int(o.end_date.timestamp() * 1000)
    return tokens, slugs, expiry


async def _select_markets() -> tuple[list[str], dict[str, str], dict[str, int]]:
    """ПРАВКА 1: набор рынков с запасом жизни >= MIN_LIFE_MS; иначе ждёт цикл."""
    while True:
        tokens, slugs, expiry = await asyncio.to_thread(_discover_live)
        now_ms = wsc.utc_ms()
        good = [t for t in tokens if expiry.get(t, 0) - now_ms >= MIN_LIFE_MS]
        if good:
            good_set = set(good)
            return (
                good,
                {t: s for t, s in slugs.items() if t in good_set},
                {t: e for t, e in expiry.items() if t in good_set},
            )
        print(
            f"[probe] живых с запасом >= {MIN_LIFE_S / 60:.0f} мин: 0; "
            f"жду новый цикл ({SELECT_POLL_S:.0f} с)",
            flush=True,
        )
        await asyncio.sleep(SELECT_POLL_S)


async def _open_markets(tokens: list[str]) -> int:
    """Сколько из tokens сейчас открыты (живые по свежему discovery)."""
    live_tokens, _slugs, _expiry = await asyncio.to_thread(_discover_live)
    live = set(live_tokens)
    return sum(1 for t in tokens if t in live)


def _read_conn_stats(db_path: Path) -> list[dict]:
    """conn_stats таблицы: все соединения прогона, по conn_id."""
    con = store.connect(db_path)
    try:
        cols = [d[0] for d in con.execute("SELECT * FROM conn_stats LIMIT 0").description]
        rows = con.execute("SELECT * FROM conn_stats ORDER BY conn_id").fetchall()
    finally:
        con.close()
    return [dict(zip(cols, r)) for r in rows]


def _classify_recons(db_path: Path) -> dict[str, int]:
    """Сверки и классификация mismatch: самозалечившийся / устойчивый.

    Самозалечивание (формулировка владельца): следующий же recon по токену
    = match и между ними не было rest_backfill/server_resync/disconnect.
    """
    con = store.connect(db_path)
    try:
        rows = con.execute(
            "SELECT ts_recv_ms, token_id, seq, verdict "
            "FROM recon_checks ORDER BY token_id, seq"
        ).fetchall()
        gaps: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for token_id, start_ms, end_ms in con.execute(
            "SELECT token_id, start_ms, end_ms FROM gap_intervals "
            "WHERE reason IN ('server_resync', 'disconnect')"
        ).fetchall():
            gaps[token_id].append((start_ms, end_ms))
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

    by_token: dict[str, list[tuple[int, bool]]] = defaultdict(list)
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
                continue
            ts_next, next_matched = checks[i + 1]
            if not next_matched:
                continue
            healed = True
            if bf_idx < len(bf):
                while bf_idx < len(bf) and bf[bf_idx] <= ts_i:
                    bf_idx += 1
                if bf_idx < len(bf) and bf[bf_idx] <= ts_next:
                    healed = False
            for start_ms, end_ms in g:
                if start_ms < ts_next and end_ms > ts_i:
                    healed = False
            if healed:
                self_healed += 1

    return {
        "total": total,
        "mismatch": mismatch_total,
        "self_healed": self_healed,
        "persistent": mismatch_total - self_healed,
    }


def _auto_n_conns(tokens: list[str], slugs: dict[str, str]) -> int:
    """Та же формула, что у коллектора при n_conns=0."""
    n_markets = len({slugs.get(t, t) for t in tokens})
    return max(1, (n_markets + wsc.MARKETS_PER_CONN - 1) // wsc.MARKETS_PER_CONN)


def _conn_tokens(tokens: list[str], slugs: dict[str, str], n_conns: int) -> list[list[str]]:
    """Разбиение токенов по соединениям (то же, что у коллектора)."""
    return wsc._partition_tokens(tokens, slugs, n_conns)


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
    runs: list[dict] = []
    for idx, (label, n_conns) in enumerate(RUN_SPEC):
        i = idx + 1
        db = Path(DB_PATTERN.format(i=i))
        while True:  # повтор невалидного прогона (ПРАВКА 2)
            tokens, slugs, expiry = await _select_markets()
            eff_n_conns = _auto_n_conns(tokens, slugs) if n_conns == 0 else n_conns
            print(f"--- прогон {i}/{len(RUN_SPEC)} ({label}) ---", flush=True)
            open_start = await _open_markets(tokens)
            start_ms = wsc.utc_ms()
            nearest_expiry_s = min(expiry[t] for t in tokens) / 1000.0 - start_ms / 1000.0
            print(
                f"[probe] токенов={len(tokens)}, соединений={eff_n_conns}, "
                f"открыто на старте={open_start}, ближайшее истечение "
                f"через {nearest_expiry_s:.0f} с",
                flush=True,
            )
            exit_code = await _run_once(
                db_path=db,
                export_root=Path(EXPORT_PATTERN.format(i=i)),
                n_conns=n_conns,
                tokens=tokens,
                slugs=slugs,
            )
            open_end = await _open_markets(tokens)
            valid = open_start > 0 and open_end >= VALIDITY_FLOOR * open_start
            print(
                f"[probe] прогон {i} exit_code={exit_code}; открыто: "
                f"старт={open_start}, финиш={open_end}; валиден={valid}",
                flush=True,
            )
            if not valid:
                print(
                    f"[probe] прогон {i} НЕВАЛИДЕН (открытых к концу {open_end} "
                    f"< {VALIDITY_FLOOR:.0%} от старта {open_start}); повторяю",
                    flush=True,
                )
                continue
            conns = _read_conn_stats(db)
            recon = _classify_recons(db)
            runs.append(
                {
                    "i": i,
                    "label": label,
                    "n_conns": eff_n_conns,
                    "exit_code": exit_code,
                    "conns": conns,
                    "recon": recon,
                    "messages": sum(int(c["messages"]) for c in conns),
                    "open_start": open_start,
                    "open_end": open_end,
                    "nearest_expiry_s": nearest_expiry_s,
                    "tokens": tokens,
                    "slugs": slugs,
                }
            )
            break

    # ---- средние по двум прогонам каждой конфигурации ----
    share_1 = sum(
        (r["recon"]["mismatch"] / r["recon"]["total"])
        if r["recon"]["total"] else 0.0
        for r in runs if r["label"] == "1conn"
    ) / max(1, sum(1 for r in runs if r["label"] == "1conn"))
    share_2 = sum(
        (r["recon"]["mismatch"] / r["recon"]["total"])
        if r["recon"]["total"] else 0.0
        for r in runs if r["label"] == "2conn"
    ) / max(1, sum(1 for r in runs if r["label"] == "2conn"))

    msgs_1 = sum(r["messages"] for r in runs if r["label"] == "1conn") / max(
        1, sum(1 for r in runs if r["label"] == "1conn")
    )
    msgs_2 = sum(r["messages"] for r in runs if r["label"] == "2conn") / max(
        1, sum(1 for r in runs if r["label"] == "2conn")
    )

    # ---- критерии ----
    c2_fail = [r["i"] for r in runs if r["recon"]["persistent"] > 0]
    c2_stop = [
        r["i"]
        for r in runs
        if r["recon"]["total"] > 0
        and r["recon"]["self_healed"] / r["recon"]["total"] > SELF_HEAL_LIMIT
    ]
    c3_ok = share_2 <= FAIL_MULTIPLIER * share_1
    c4_bad: list[tuple[int, int]] = []  # (прогон, conn_id)
    for r in runs:
        for c in r["conns"]:
            if int(c["n_tokens"]) > 0 and float(c["max_silence_s"]) > SILENCE_FAIL_S:
                c4_bad.append((r["i"], int(c["conn_id"])))
    c5_ok = msgs_2 >= MESSAGE_FLOOR * msgs_1
    c5_bad_runs = [
        r["i"] for r in runs if r["messages"] < MESSAGE_FLOOR * msgs_1
    ] if not c5_ok else []

    # ---- отчёт ----
    print("\n================ ОТЧЁТ ПРИЁМКИ (per-connection) ================", flush=True)
    for r in runs:
        print(
            f"\nПРОГОН {r['i']} ({r['label']}, соединений {r['n_conns']}):",
            flush=True,
        )
        print(
            f"  открыто рынков: старт={r['open_start']}, финиш={r['open_end']}; "
            f"ближайшее истечение на старте: {r['nearest_expiry_s']:.0f} с",
            flush=True,
        )
        for c in r["conns"]:
            print(
                f"  conn {int(c['conn_id']) + 1}: токенов={int(c['n_tokens'])}, "
                f"messages={int(c['messages'])}, events={int(c['events'])}, "
                f"recons={int(c['recons'])}, mismatch={int(c['recons_mismatch'])}, "
                f"max_silence_s={float(c['max_silence_s']):.2f}, "
                f"эпизодов={int(c['n_silence_episodes'])}, "
                f"пингов={int(c['n_pings_fired'])}, "
                f"первое={c['first_msg_ms']}, последнее={c['last_msg_ms']}",
                flush=True,
            )
        rc = r["recon"]
        print(
            f"  сверки: всего={rc['total']}, mismatch={rc['mismatch']}, "
            f"самозалечившихся={rc['self_healed']}, устойчивых={rc['persistent']}",
            flush=True,
        )

    print(
        f"\nдоля mismatch (среднее по прогонам): 1conn={share_1:.5f}, "
        f"2conn={share_2:.5f}, порог C3={FAIL_MULTIPLIER * share_1:.5f}",
        flush=True,
    )
    print(
        f"сообщений (среднее по прогонам): 1conn={msgs_1:.0f}, "
        f"2conn={msgs_2:.0f}, порог C5={MESSAGE_FLOOR * msgs_1:.0f}",
        flush=True,
    )

    verdicts = {
        "C2": "FAIL" if c2_fail else "PASS",
        "C2_stop": "STOP" if c2_stop else "",
        "C3": "PASS" if c3_ok else "FAIL",
        "C4": "FAIL" if c4_bad else "PASS",
        "C5": "PASS" if c5_ok else "FAIL",
    }
    print(f"\nвердикты: {verdicts}", flush=True)

    if c2_fail:
        print(f"  C2 FAIL: устойчивые mismatch в прогонах {c2_fail}.", flush=True)
    if c2_stop:
        print(f"  C2 STOP: самозалечившихся >5% сверок в прогонах {c2_stop}.", flush=True)
    if not c3_ok:
        print(
            f"  C3 FAIL: 2conn={share_2:.5f} > 1.5 x 1conn={share_1:.5f} "
            f"({FAIL_MULTIPLIER * share_1:.5f}).",
            flush=True,
        )
    if c4_bad:
        for run_i, conn_id in c4_bad:
            r = runs[run_i - 1]
            parts = _conn_tokens(r["tokens"], r["slugs"], r["n_conns"])
            toks = parts[conn_id] if conn_id < len(parts) else []
            print(
                f"  C4 FAIL: прогон {run_i}, соединение {conn_id + 1} молчало "
                f"> {SILENCE_FAIL_S:.0f} с при непустом наборе. "
                f"Токены ({len(toks)}): {toks}",
                flush=True,
            )
    if not c5_ok:
        print(
            f"  C5 FAIL: 2conn={msgs_2:.0f} < 0.7 x 1conn={msgs_1:.0f} "
            f"({MESSAGE_FLOOR * msgs_1:.0f}). Слабые прогоны 2conn: {c5_bad_runs}.",
            flush=True,
        )
        for r in runs:
            if r["i"] in c5_bad_runs:
                for c in r["conns"]:
                    print(
                        f"    прогон {r['i']}, соединение {int(c['conn_id']) + 1}: "
                        f"messages={int(c['messages'])}",
                        flush=True,
                    )

    if c2_stop:
        print("СТОП: самозалечившихся больше 5% от всех сверок (гонка массовая).", flush=True)
        return 2
    if c2_fail or not c3_ok or c4_bad or not c5_ok:
        print("ПРОВАЛ: нарушен критерий приёмки (см. выше).", flush=True)
        return 1
    print("ПРИЁМКА ПРОЙДЕНА.", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
