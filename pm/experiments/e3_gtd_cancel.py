"""Э3. GTD-ордера, минимальный TTL, p99 задержки DELETE /order,
и главный вопрос -- цена неудавшегося расчёта и риск pauseUser.

ЭТО ЕДИНСТВЕННЫЙ МОДУЛЬ ПРОЕКТА С ЗАПИСЬЮ. Он никогда не вызывается
из обычного прогона probe.py: требует явного --run-e3 и ввода фразы
подтверждения с клавиатуры.

Статус знания:
- (а) Ончейн-отмены нет; есть операторский pauseUser. expiration убран из
      подписи EIP-712, но остался в теле POST /order.
- (б) ВЕРОЯТНО: если expiration не подписан, то он НЕ может быть гарантией
      протокольного уровня. Он становится обещанием оффчейн-оператора.
      Поэтому "поддерживается ли GTD" распадается на два разных вопроса:
      (i) принимает ли сервер поле; (ii) исчезает ли ордер фактически.
      Мерить надо (ii), а не (i).
- (в) ПРЕДПОЛОЖЕНИЕ: санкция за failed settlement нигде не документирована
      численно. Никакой эмпирический тест на своём аккаунте не должен
      проводиться: ставка -- весь аккаунт.

ПОЗИЦИЯ ПО РИСКУ (явная, чтобы не было соблазна передумать потом):
Отзыв allowance как "аварийная отмена всего" запрещён в этом проекте.
С точки зрения оператора отзыв allowance при живых ордерах неотличим от
умышленного срыва расчёта (матчинг прошёл, трансфер провалился), то есть
ровно от поведения, против которого pauseUser и существует. Единственный
допустимый kill-switch: DELETE всех ордеров, затем верификация пустого
списка, и только потом -- если вообще нужно -- работа с allowance.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

CONFIRM_PHRASE = "I ACCEPT ORDER PLACEMENT RISK"


class OrderClient(Protocol):
    """Минимальный контракт, который должна закрывать обёртка polymarket-client.

    Намеренно свой Protocol, а не прямой импорт SDK: точные имена методов
    polymarket-client в этом проекте ещё не верифицированы (статус (в)).
    Обёртка пишется однажды в pm/broker.py после чтения исходников SDK.
    py-clob-client не используется ни при каких условиях.
    """

    def post_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST /order. Возвращает тело ответа."""
        ...

    def delete_order(self, order_id: str) -> dict[str, Any]:
        """DELETE /order."""
        ...

    def get_open_orders(self) -> list[dict[str, Any]]:
        """GET списка активных ордеров."""
        ...


@dataclass(slots=True)
class GtdProbeResult:
    """Итог одной попытки GTD с заданным TTL."""

    ttl_seconds: int
    accepted_by_server: bool
    order_id: str | None
    disappeared_within_ttl: bool | None
    observed_lifetime_s: float | None
    error: str | None


@dataclass(slots=True)
class E3Report:
    """Итог Э3."""

    gtd_probes: list[dict[str, Any]] = field(default_factory=list)
    cancel_latencies_ms: list[float] = field(default_factory=list)
    cancel_p50_ms: float | None = None
    cancel_p99_ms: float | None = None
    min_working_ttl_s: int | None = None
    settlement_risk_note: str = ""
    executed: bool = False


def percentile(values: list[float], q: float) -> float | None:
    """Персентиль методом nearest-rank.

    Args:
        values: выборка.
        q: доля в (0, 1], например 0.99.

    Returns:
        Значение либо None для пустой выборки.

    Raises:
        ValueError: если q вне (0, 1].
    """
    if not (0 < q <= 1):
        raise ValueError("q must be in (0,1]")
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(q * len(s))) - 1))
    return s[idx]


def p99_note(n: int) -> str:
    """Честное предупреждение о смысле p99 на малой выборке.

    Оценить p99 можно только при n >= ~300 измерениях; один тестовый ордер
    даёт только порядок величины.
    """
    if n >= 300:
        return f"n={n}: p99 интерпретируем."
    return (
        f"n={n}: это НЕ p99, а максимум малой выборки. Для p99 нужно >= 300 "
        "измерений отмены, накопленных в штатной работе терминала (цель A), "
        "а не в разовом тесте."
    )


def settlement_risk_assessment() -> str:
    """Фиксированная оценка риска failed settlement / pauseUser.

    Возвращает текст, который попадает в отчёт дословно, чтобы вывод
    нельзя было переформулировать позже.
    """
    return (
        "(в) Санкция за failed settlement не имеет известного численного выражения. "
        "Наблюдаемый механизм -- операторский pauseUser, то есть решение человека "
        "или внутреннего правила, а не детерминированный штраф. Обратимость "
        "НЕИЗВЕСТНА и не подлежит экспериментальной проверке на рабочем аккаунте: "
        "ошибка невосстановима и уничтожает цель A. Практические следствия, "
        "принимаемые как правила проекта: "
        "(1) отзыв allowance при живых ордерах запрещён; "
        "(2) kill-switch = DELETE всех ордеров + проверка пустого списка; "
        "(3) свободный баланс pUSD всегда строго >= сумма открытых обязательств; "
        "(4) лимит одновременных ордеров таков, что одновременное исполнение всех "
        "остаётся полностью покрытым. Эти правила не зависят от исхода цели B."
    )


def run_readonly() -> E3Report:
    """Безопасная часть Э3: только фиксация оценки риска, без сети и без ордеров.

    Именно эта ветка вызывается из обычного прогона probe.py.
    """
    return E3Report(settlement_risk_note=settlement_risk_assessment(), executed=False)


def run_live_order_test(
    client: OrderClient,
    token_id: str,
    price: float,
    size: float,
    ttl_candidates: tuple[int, ...] = (10, 30, 60, 300),
    confirm: str = "",
    poll_interval_s: float = 1.0,
    now: Callable[[], float] = time.time,
) -> E3Report:
    """ЕДИНСТВЕННАЯ ФУНКЦИЯ ПРОЕКТА, КОТОРАЯ СТАВИТ ОРДЕРА.

    Сценарий на каждый TTL:
    1. Поставить минимальный лимитный ордер С ЗАВЕДОМО НЕИСПОЛНИМОЙ ЦЕНОЙ
       (далеко от рынка), чтобы тест не создавал позицию и не платил комиссию.
    2. Проверить, принят ли expiration сервером (вопрос i).
    3. Опросить список открытых ордеров до TTL + 30 с и зафиксировать, исчез ли
       ордер фактически (вопрос ii -- единственный, который имеет смысл).
    4. Если не исчез -- удалить вручную и измерить задержку DELETE.
    5. В любом исходе в finally гарантированно снять ордер.

    Args:
        client: обёртка над polymarket-client.
        token_id: token_id инструмента.
        price: цена; ОБЯЗАНА быть заведомо неисполнимой (например 0.01 на bid).
        size: минимальный размер.
        ttl_candidates: проверяемые TTL в секундах, по возрастанию.
        confirm: должно точно равняться CONFIRM_PHRASE.
        poll_interval_s: шаг опроса списка ордеров.
        now: источник времени (инъектируется в тестах).

    Returns:
        E3Report с измерениями.

    Raises:
        PermissionError: если confirm не совпадает с CONFIRM_PHRASE.
        ValueError: если price/size выглядят опасно (цена близко к середине).
    """
    if confirm != CONFIRM_PHRASE:
        raise PermissionError(
            "Э3 требует явного подтверждения. Передайте confirm="
            f"{CONFIRM_PHRASE!r}. Ставить ордера без этого запрещено."
        )
    if not (0 < price < 1):
        raise ValueError("price must be in (0,1)")
    if 0.2 <= price <= 0.8:
        raise ValueError(
            "Цена слишком близка к рынку: тестовый ордер не должен иметь шанса "
            "исполниться. Используйте крайнюю цену, например 0.01."
        )
    if size <= 0:
        raise ValueError("size must be > 0")

    report = E3Report(settlement_risk_note=settlement_risk_assessment(), executed=True)
    for ttl in ttl_candidates:
        order_id: str | None = None
        probe = GtdProbeResult(ttl, False, None, None, None, None)
        try:
            payload = {
                "tokenID": token_id,
                "price": str(price),
                "size": str(size),
                "side": "BUY",
                "orderType": "GTD",
                # (а) expiration остался в теле POST /order, хотя убран из подписи.
                "expiration": str(int(now()) + ttl),
            }
            resp = client.post_order(payload)
            order_id = (
                resp.get("orderID") or resp.get("orderId") or resp.get("id")
            )
            probe.accepted_by_server = bool(
                resp.get("success", order_id is not None)
            )
            probe.order_id = order_id
            if order_id is None:
                probe.error = f"Сервер не вернул id ордера: {resp}"
                report.gtd_probes.append(asdict(probe))
                continue

            placed_at = now()
            deadline = placed_at + ttl + 30
            disappeared = False
            while now() < deadline:
                time.sleep(poll_interval_s)
                ids = {
                    o.get("orderID") or o.get("orderId") or o.get("id")
                    for o in client.get_open_orders()
                }
                if order_id not in ids:
                    disappeared = True
                    probe.observed_lifetime_s = now() - placed_at
                    break
            probe.disappeared_within_ttl = disappeared
            if (
                disappeared
                and report.min_working_ttl_s is None
                and probe.observed_lifetime_s is not None
                and probe.observed_lifetime_s <= ttl + 30
            ):
                report.min_working_ttl_s = ttl
        except Exception as exc:  # noqa: BLE001 - любой сбой фиксируем как данные
            probe.error = repr(exc)
            log.exception("Э3: ошибка при TTL=%s", ttl)
        finally:
            if order_id is not None:
                t0 = time.perf_counter()
                try:
                    client.delete_order(order_id)
                    report.cancel_latencies_ms.append(
                        (time.perf_counter() - t0) * 1000
                    )
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "НЕ УДАЛОСЬ СНЯТЬ ордер %s: %r. Снимите вручную НЕМЕДЛЕННО.",
                        order_id,
                        exc,
                    )
            report.gtd_probes.append(asdict(probe))

    lat = report.cancel_latencies_ms
    report.cancel_p50_ms = statistics.median(lat) if lat else None
    report.cancel_p99_ms = percentile(lat, 0.99)
    report.settlement_risk_note += " | " + p99_note(len(lat))
    return report
