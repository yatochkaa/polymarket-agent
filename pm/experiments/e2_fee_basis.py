"""Э2. База комиссии Polymarket: shares или notional.

Модуль восстанавливает контракт только по местам вызова в probe.py и
по правилам pm.fees. До подтверждения результата Э2 он не угадывает одну
комиссию, а возвращает структурированный вывод с явным статусом.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any, Literal

from ..config import Settings
from ..fees import Vertical, _fee_notional_basis, _fee_shares_basis, fee_rate

Basis = Literal["shares", "notional", "unknown"]

# Критерий различимости: гипотеза разрешается только если проигравшая
# остаточная ошибка не меньше чем в MIN_RESIDUAL_RATIO раз больше выигравшей,
# а выигравшая ошибка мала относительно суммарного объёма комиссий. Число 2.0
# выбрано как минимальное устойчивое различие, которое не закрывает гейт на
# почти равных остатках.
MIN_RESIDUAL_RATIO = Decimal("2.0")
MIN_RESIDUAL_SHARE = Decimal("0.01")


@dataclass(slots=True)
class E2Result:
    basis: Basis
    n_observations: int
    residual_shares: float
    residual_notional: float
    residual_ratio: float
    fee_rate: float
    vertical: str
    resolved: bool
    evidence: list[dict[str, Any]]


def decode_orderfilled(rpc_url: str, tx: str) -> dict[str, Any]:
    raise NotImplementedError(
        "Э2: декодирование OrderFilled не реализовано; ончейн-подтверждение базы комиссии недоступно"
    )


def _residual_for_basis(row: dict[str, Any], rate: Decimal, fn) -> Decimal:
    shares = Decimal(str(row.get("shares", 0)))
    price = Decimal(str(row.get("price", 0)))
    fee = Decimal(str(row.get("fee", 0)))
    return abs(fee - fn(shares, price, rate))


def run(s: Settings, data: Any, *, vertical: Vertical) -> E2Result:
    rate = fee_rate(vertical)
    observations = list(data or [])
    residuals: list[dict[str, Any]] = []
    total_shares = Decimal(0)
    total_notional = Decimal(0)
    total_fee = Decimal(0)
    for row in observations:
        shares = Decimal(str(row.get("shares", 0)))
        price = Decimal(str(row.get("price", 0)))
        fee = Decimal(str(row.get("fee", 0)))
        total_fee += fee
        rs = _residual_for_basis(row, rate, _fee_shares_basis)
        rn = _residual_for_basis(row, rate, _fee_notional_basis)
        total_shares += rs
        total_notional += rn
        residuals.append({"tx": row.get("tx"), "shares_residual": float(rs), "notional_residual": float(rn)})

    basis: Basis = "unknown"
    resolved = False
    ratio = Decimal(0)
    if observations:
        bigger = max(total_shares, total_notional)
        smaller = min(total_shares, total_notional)
        ratio = (bigger / smaller) if smaller != 0 else Decimal("Infinity")
        if bigger != 0 and ratio >= MIN_RESIDUAL_RATIO and smaller <= total_fee * MIN_RESIDUAL_SHARE:
            basis = "shares" if total_shares < total_notional else "notional"
            resolved = True
    return E2Result(
        basis=basis,
        n_observations=len(observations),
        residual_shares=float(total_shares),
        residual_notional=float(total_notional),
        residual_ratio=float(ratio),
        fee_rate=float(rate),
        vertical=vertical.value if hasattr(vertical, "value") else str(vertical),
        resolved=resolved,
        evidence=residuals,
    )


def report_dict(r2: E2Result) -> dict[str, Any]:
    return asdict(r2)


__all__ = ["E2Result", "decode_orderfilled", "run", "report_dict", "MIN_RESIDUAL_RATIO", "MIN_RESIDUAL_SHARE"]
