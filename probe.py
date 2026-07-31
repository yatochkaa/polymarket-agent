#!/usr/bin/env python3
"""probe.py -- исполняемый разведочный скрипт для Э1, Э2, Э4 (только чтение).

Э3 разделён на две части:
- безопасная (оценка риска failed settlement) выполняется всегда;
- живой тест ордера требует --run-e3 И ввода фразы подтверждения с клавиатуры
  И готовой обёртки pm/broker.py над polymarket-client (её в архиве НЕТ
  намеренно: писать код подписи по памяти = гарантированный баг).

Примеры:
    python probe.py --all
    python probe.py --e1 --hours 24 --markets 16
    python probe.py --e2 --tx 0xabc...        # ончейн-путь Э2
    python probe.py --e4 --window-days 90
    python probe.py --run-e3 --token-id 123 --price 0.01 --size 5

Все артефакты пишутся в data/ с меткой времени UTC и НИКОГДА не перезаписываются:
сравнение двух прогонов -- часть метода, а не случайность.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pm.config import (
    CLOB_V2_LIVE_FROM,
    EXCHANGE_V2,
    NEG_RISK_EXCHANGE_V2,
    ORDERBOOK_HISTORY_DEAD_FROM,
    Settings,
    load_settings,
)
from pm.experiments import e1_prices_history as e1
from pm.experiments import e2_fee_basis as e2
from pm.experiments import e3_gtd_cancel as e3
from pm.experiments import e4_tennis as e4
from pm.fees import Vertical, resolved_basis
from pm.httpc import HttpFailure, ReadClient
from pm.store import utc_stamp, write_json

log = logging.getLogger("probe")


def build_clients(s: Settings, stamp: str) -> tuple[ReadClient, ReadClient, ReadClient]:
    """Создаёт три read-only клиента (CLOB, Gamma, Data) с общим сырым журналом."""
    raw_log = s.data_dir / f"raw_{stamp}.jsonl"
    mk = lambda host: ReadClient(  # noqa: E731 - короткая фабрика
        host,
        timeout_s=s.request_timeout_s,
        max_retries=s.max_retries,
        rate_limit_rps=s.rate_limit_rps,
        raw_log=raw_log,
    )
    return mk(s.clob_host), mk(s.gamma_host), mk(s.data_host)


def preflight(s: Settings, clob: ReadClient, gamma: ReadClient) -> dict[str, Any]:
    """Проверяет базовые предположения до запуска экспериментов.

    Проверяется:
    1. жив ли CLOB (любой из пробных эндпоинтов отвечает);
    2. мёртв ли /orderbook-history ИМЕННО тихо (200 + пустота), а не через 404 --
       это различие важно для детекторов деградации в цели A;
    3. вернул ли Gamma хоть один бинарный рынок с двумя token_id.

    Returns:
        dict, который кладётся в отчёт. Не бросает исключений: проваленный
        preflight -- тоже результат.
    """
    out: dict[str, Any] = {
        "clob_v2_live_from": CLOB_V2_LIVE_FROM,
        "exchange_v2": EXCHANGE_V2,
        "neg_risk_exchange_v2": NEG_RISK_EXCHANGE_V2,
        "orderbook_history_dead_from": ORDERBOOK_HISTORY_DEAD_FROM,
        "fee_basis_resolved": resolved_basis(),
    }
    try:
        env = clob.get("/ok", None)
        out["clob_ok"] = {"status": env.status, "payload": env.payload}
    except HttpFailure as exc:
        out["clob_ok"] = {"error": str(exc)}
    try:
        env = clob.get("/orderbook-history", {"market": "probe"})
        out["orderbook_history"] = {
            "status": env.status,
            "empty": env.is_empty,
            "silent_death_confirmed": env.status == 200 and env.is_empty,
        }
    except HttpFailure as exc:
        out["orderbook_history"] = {
            "error": str(exc),
            "silent_death_confirmed": False,
            "note": "Ответ не 200: отказ громкий, а не тихий.",
        }
    try:
        env = gamma.get("/markets", {"limit": 5, "closed": "false"})
        payload = env.payload
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        out["gamma_markets_sample"] = {
            "status": env.status,
            "n": len(rows) if isinstance(rows, list) else 0,
        }
    except HttpFailure as exc:
        out["gamma_markets_sample"] = {"error": str(exc)}
    return out


def main(argv: list[str] | None = None) -> int:
    """Точка входа CLI.

    Returns:
        Код возврата процесса: 0 при успешном выполнении выбранных блоков
        (включая случай, когда вердикт "unknown" -- это легальный результат),
        2 при ошибке конфигурации или непройденном подтверждении Э3.
    """
    p = argparse.ArgumentParser(description="Polymarket recon probe (read-only)")
    p.add_argument("--all", action="store_true", help="Э1 + Э2 + Э4 + Э3(readonly)")
    p.add_argument("--e1", action="store_true")
    p.add_argument("--e2", action="store_true")
    p.add_argument("--e4", action="store_true")
    p.add_argument("--hours", type=int, default=24, help="окно для Э1, часы")
    p.add_argument("--markets", type=int, default=12, help="сколько рынков в Э1")
    p.add_argument("--fidelity", type=int, default=1)
    p.add_argument("--window-days", type=int, default=90, help="окно Э4")
    p.add_argument("--tx", type=str, default=None, help="tx hash для ончейн-пути Э2")
    p.add_argument(
        "--vertical",
        type=str,
        default="sports",
        choices=[v.value for v in Vertical],
        help="вертикаль для расчёта комиссии в Э2",
    )
    p.add_argument("--run-e3", action="store_true", help="ЖИВОЙ тестовый ордер")
    p.add_argument("--token-id", type=str, default=None)
    p.add_argument("--price", type=float, default=0.01)
    p.add_argument("--size", type=float, default=5.0)
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    run_e1 = args.e1 or args.all
    run_e2 = args.e2 or args.all
    run_e4 = args.e4 or args.all
    if not any([run_e1, run_e2, run_e4, args.run_e3]):
        p.error("Не выбран ни один блок. Используйте --all или --e1/--e2/--e4.")

    s = load_settings()
    stamp = utc_stamp()
    clob, gamma, data = build_clients(s, stamp)
    report: dict[str, Any] = {"stamp": stamp, "preflight": preflight(s, clob, gamma)}

    try:
        if run_e1:
            end_ts = int(time.time())
            start_ts = end_ts - args.hours * 3600
            log.info("Э1: окно %s..%s, рынков %s", start_ts, end_ts, args.markets)
            try:
                r1 = e1.run(
                    s,
                    clob,
                    gamma,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    n_markets=args.markets,
                    fidelity=args.fidelity,
                )
                report["e1"] = asdict(r1)
            except Exception as exc:  # noqa: BLE001 - фиксируем, не маскируем
                log.exception("Э1 упал")
                report["e1"] = {"error": repr(exc)}

        if run_e2:
            log.info("Э2: API-путь")
            try:
                r2 = e2.run(s, data, vertical=Vertical(args.vertical))
                report["e2"] = e2.report_dict(r2)
            except Exception as exc:  # noqa: BLE001
                log.exception("Э2 (API) упал")
                report["e2"] = {"error": repr(exc)}
            if args.tx:
                try:
                    report["e2_onchain"] = e2.decode_orderfilled(s.rpc_url, args.tx)
                except Exception as exc:  # noqa: BLE001
                    log.exception("Э2 (onchain) упал")
                    report["e2_onchain"] = {"error": repr(exc)}

        if run_e4:
            log.info("Э4: теннис, окно %s дней", args.window_days)
            try:
                r4 = e4.run(s, gamma, window_days=args.window_days)
                report["e4"] = e4.report_dict(r4)
            except Exception as exc:  # noqa: BLE001
                log.exception("Э4 упал")
                report["e4"] = {"error": repr(exc)}

        report["e3_readonly"] = asdict(e3.run_readonly())

        if args.run_e3:
            if not args.token_id:
                log.error("--run-e3 требует --token-id")
                return 2
            print("\n" + "=" * 72)
            print("ВНИМАНИЕ: будет выставлен РЕАЛЬНЫЙ ордер на ваш аккаунт.")
            print(f"token_id={args.token_id} price={args.price} size={args.size}")
            print("Цена должна быть заведомо неисполнимой.")
            print(f"Для продолжения введите точно: {e3.CONFIRM_PHRASE}")
            print("=" * 72)
            typed = input("> ").strip()
            if typed != e3.CONFIRM_PHRASE:
                log.error("Подтверждение не совпало. Ордер НЕ выставлен.")
                write_json(s.data_dir / f"probe_{stamp}.json", report)
                return 2
            try:
                from pm.broker import build_order_client  # пишется после чтения SDK
            except ImportError:
                log.error(
                    "pm/broker.py отсутствует. Это намеренно: обёртка над "
                    "polymarket-client пишется после чтения его исходников, а не "
                    "по памяти. См. ASSUMPTIONS.md, раздел Открытые вопросы SDK."
                )
                write_json(s.data_dir / f"probe_{stamp}.json", report)
                return 2
            client = build_order_client(s)
            r3 = e3.run_live_order_test(
                client,
                token_id=args.token_id,
                price=args.price,
                size=args.size,
                confirm=typed,
            )
            report["e3_live"] = asdict(r3)
    finally:
        clob.close()
        gamma.close()
        data.close()

    out = write_json(s.data_dir / f"probe_{stamp}.json", report)
    print(json.dumps({"artifact": str(out)}, ensure_ascii=False))
    print(summarize(report))
    return 0


def summarize(report: dict[str, Any]) -> str:
    """Короткая сводка для терминала, без интерпретаций сверх данных."""
    lines = ["", "СВОДКА", "------"]
    e1r = report.get("e1", {})
    if "verdict" in e1r:
        lines.append(
            f"Э1 prices-history: {e1r['verdict']} "
            f"(sigma={e1r.get('pooled_sigma_sum')}, "
            f"mean|dev|={e1r.get('pooled_mean_abs_dev')}, "
            f"рынков с данными {e1r.get('n_markets_with_data')}/"
            f"{e1r.get('n_markets_tested')})"
        )
    else:
        lines.append(f"Э1: не выполнен или ошибка: {e1r.get('error')}")
    e2r = report.get("e2", {})
    lines.append(
        f"Э2 fee basis: {e2r.get('verdict', 'n/a')} "
        f"(usable={e2r.get('n_usable')}, blocker={e2r.get('blocking_reason')})"
    )
    e4r = report.get("e4", {})
    if "n_markets" in e4r:
        lines.append(
            f"Э4 теннис: рынков {e4r['n_markets']}, разрешено {e4r['n_resolved']}, "
            f"в неделю {e4r.get('resolutions_per_week')}, "
            f"споры измеримы: {e4r.get('dispute_share_is_measurable')}"
        )
        lines.append(f"   МОЩНОСТЬ: {e4r.get('power_note')}")
    else:
        lines.append(f"Э4: не выполнен или ошибка: {e4r.get('error')}")
    lines.append(
        "Гейты: см. PREREGISTRATION.md раздел 6. Ни один вывод об edge не делается "
        "в этом скрипте: probe.py измеряет инфраструктуру, а не гипотезу B."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    sys.exit(main())
