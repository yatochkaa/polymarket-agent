"""Восстановление книги из дельт и проверка целостности (recon_checks).

LiveBook держит состояние книги токена, собираемое из WS-сообщений:
`book` — полная замена, `price_change` — изменение одного уровня.
При каждом полном серверном снимке `book` LiveBook сравнивается со снимком;
расхождение (mismatch) = потеря/дубль сообщений. Это лучший детектор потерь,
поскольку серверного seq не существует.

Стороны: BUY меняет bid-сторону, SELL — ask-сторону (подтверждено в
задачах 3.5/3.6 проверочного контура, ASSUMPTIONS.md).
"""

from __future__ import annotations

from typing import Any, Sequence

from . import schema
from .schema import SIDE_BUY, SIDE_SELL

TICK = 0.01  # шаг цены crypto up/down; recon требует точного совпадения, не < tick
VWAP_QUANTITY = 100.0


class LiveBook:
    """Состояние книги одного token_id, инкрементальное, O(1) на дельту."""

    def __init__(self) -> None:
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.initialized = False

    def set_book(
        self,
        bids: Sequence[tuple[float, float]],
        asks: Sequence[tuple[float, float]],
    ) -> None:
        """Полный снимок: заменяет обе стороны."""
        self.bids = {p: s for p, s in bids if s > 0}
        self.asks = {p: s for p, s in asks if s > 0}
        self.initialized = True

    def set_book_from_dicts(
        self,
        bids: dict[float, float],
        asks: dict[float, float],
    ) -> None:
        self.bids = {p: s for p, s in bids.items() if s is not None and s > 0}
        self.asks = {p: s for p, s in asks.items() if s is not None and s > 0}
        self.initialized = True

    def apply_change(self, side: str, price: float, size: float) -> None:
        """Дельта: BUY -> bid-сторона, SELL -> ask-сторона. size<=0 снимает уровень."""
        levels = self.bids if side == SIDE_BUY else self.asks
        if size is None or size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        price = max(self.bids)
        return (price, self.bids[price])

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        price = min(self.asks)
        return (price, self.asks[price])

    def vwap(self, side: str, quantity: float = VWAP_QUANTITY) -> float | None:
        """VWAP первых `quantity` контрактов; None если глубины мало."""
        levels = self.bids if side == "bid" else self.asks
        if not levels:
            return None
        ordered = sorted(levels.items(), key=lambda kv: kv[0], reverse=(side == "bid"))
        num = 0.0
        total = 0.0
        remaining = quantity
        for price, size in ordered:
            if size <= 0:
                continue
            take = min(remaining, size)
            num += price * take
            total += take
            remaining -= take
            if remaining <= 0:
                return num / total
        return None

    @property
    def n_levels(self) -> int:
        return len(self.bids) + len(self.asks)


def recon_check(
    *,
    ts_recv_ms: int,
    token_id: str,
    ours: LiveBook,
    theirs_bids: dict[float, float],
    theirs_asks: dict[float, float],
) -> dict[str, Any]:
    """Строка recon_checks для одного серверного снимка.

    ours — LiveBook ДО применения этого снимка (книга из дельт).
    theirs_bids/theirs_asks — полный серверный снимок.

    verdict:
      warmup   — наша книга ещё не инициализирована (первый снимок после
                 подписки/ресинка): сравнивать не с чем, не потеря;
      match    — книги идентичны (число уровней, лучшие цены, размеры на
                 общих ценах — всё совпадает);
      mismatch — расхождение: потеря/дубль сообщений.
    """
    if not ours.initialized:
        return {
            "ts_recv_ms": ts_recv_ms,
            "token_id": token_id,
            "n_levels_ours": ours.n_levels,
            "n_levels_theirs": len(theirs_bids) + len(theirs_asks),
            "max_abs_diff_price": 0.0,
            "max_abs_diff_size": 0.0,
            "verdict": "warmup",
        }

    ob = ours.best_bid()
    tb = (
        (max(theirs_bids), theirs_bids[max(theirs_bids)])
        if theirs_bids
        else None
    )
    oa = ours.best_ask()
    ta = (
        (min(theirs_asks), theirs_asks[min(theirs_asks)])
        if theirs_asks
        else None
    )
    max_price_diff = 0.0
    if ob is not None and tb is not None:
        max_price_diff = max(max_price_diff, abs(ob[0] - tb[0]))
    if oa is not None and ta is not None:
        max_price_diff = max(max_price_diff, abs(oa[0] - ta[0]))

    max_size_diff = 0.0
    for ours_levels, theirs_levels in (
        (ours.bids, theirs_bids),
        (ours.asks, theirs_asks),
    ):
        for price in set(ours_levels) & set(theirs_levels):
            max_size_diff = max(max_size_diff, abs(ours_levels[price] - theirs_levels[price]))

    n_ours = ours.n_levels
    n_theirs = len(theirs_bids) + len(theirs_asks)
    verdict = (
        "match"
        if (n_ours == n_theirs and max_price_diff == 0.0 and max_size_diff == 0.0)
        else "mismatch"
    )
    return {
        "ts_recv_ms": ts_recv_ms,
        "token_id": token_id,
        "n_levels_ours": n_ours,
        "n_levels_theirs": n_theirs,
        "max_abs_diff_price": round(max_price_diff, 8),
        "max_abs_diff_size": round(max_size_diff, 8),
        "verdict": verdict,
    }
