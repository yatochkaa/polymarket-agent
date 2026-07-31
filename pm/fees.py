"""Единственный источник истины по комиссиям Polymarket CLOB V2.

ПРАВИЛО ПРОЕКТА: числовые значения feeRate не хардкодятся больше нигде.
Любой модуль, которому нужна комиссия, импортирует функции отсюда.

Статус знаний (обязательная маркировка):
- (а) ПОДТВЕРЖДЕНО пользователем/докой: формула fee = C * feeRate * p * (1-p);
      мейкер не платит; ставки Crypto 0.07 / Sports 0.05 / Politics 0.04 /
      Geopolitics 0.
- (в) ПРЕДПОЛОЖЕНИЕ: что именно есть C -- число долей (shares) или ноционал
      в USD (shares * p). Это предмет эксперимента Э2. До получения результата
      Э2 модуль ОТКАЗЫВАЕТСЯ выдавать одно число и выдаёт интервал
      (fee_bracket), либо требует явного basis.

Почему это важно: при p = 0.20 разница между двумя трактовками -- ровно 5x
по величине комиссии. Любой расчёт edge, сделанный до Э2, обязан нести
bracket_width, иначе исход исследования автоматически UNDECIDABLE.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Final, Literal

__all__ = [
    "Vertical",
    "FeeBasis",
    "FeeQuote",
    "taker_fee",
    "maker_fee",
    "fee_bracket",
    "resolved_basis",
    "record_e2_result",
    "fee_rate",
]


class Vertical(str, Enum):
    """Вертикаль рынка. Строки совпадают с внутренними ключами проекта,
    а не обязательно с тегами Gamma API -- сопоставление делает pm.markets."""

    CRYPTO = "crypto"
    SPORTS = "sports"
    POLITICS = "politics"
    GEOPOLITICS = "geopolitics"


FeeBasis = Literal["shares", "notional", "unknown"]

# (а) подтверждено вводными проекта.
_TAKER_FEE_RATE: Final[dict[Vertical, Decimal]] = {
    Vertical.CRYPTO: Decimal("0.07"),
    Vertical.SPORTS: Decimal("0.05"),
    Vertical.POLITICS: Decimal("0.04"),
    Vertical.GEOPOLITICS: Decimal("0"),
}

# Файл, который пишет Э2. Пока его нет -- basis == "unknown".
E2_RESULT_PATH: Final[Path] = Path("data/e2_fee_basis.json")


class FeeBasisUnresolved(RuntimeError):
    """Поднимается, когда кто-то просит точную комиссию до завершения Э2."""


@dataclass(frozen=True, slots=True)
class FeeQuote:
    """Оценка комиссии тейкера с явной неопределённостью.

    Attributes:
        low: минимально возможная комиссия при любой из трактовок C.
        high: максимально возможная комиссия.
        point: точечная оценка, если basis известен, иначе None.
        basis: использованная трактовка C.
    """

    low: Decimal
    high: Decimal
    point: Decimal | None
    basis: FeeBasis

    @property
    def bracket_width(self) -> Decimal:
        """Ширина интервала неопределённости комиссии (в USD)."""
        return self.high - self.low


def fee_rate(vertical: Vertical) -> Decimal:
    """Возвращает feeRate тейкера для вертикали.

    Raises:
        KeyError: если вертикаль неизвестна. Намеренно не подставляем default:
            неизвестная вертикаль -- это ошибка классификации рынка, а не 0.
    """
    return _TAKER_FEE_RATE[Vertical(vertical)]


def resolved_basis(path: Path | None = None) -> FeeBasis:
    """Читает результат Э2 с диска.

    Returns:
        "shares" | "notional" -- если Э2 завершён и записал вывод.
        "unknown" -- если файла нет или он не проходит валидацию.
    """
    p = path or E2_RESULT_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "unknown"
    basis = raw.get("basis")
    if basis in ("shares", "notional"):
        return basis  # type: ignore[return-value]
    return "unknown"


def record_e2_result(
    basis: FeeBasis,
    evidence: dict[str, object],
    path: Path | None = None,
) -> Path:
    """Фиксирует вывод Э2 на диске (append-only по смыслу: перезапись требует
    ручного удаления файла, чтобы нельзя было тихо переобъявить результат).

    Args:
        basis: "shares" или "notional".
        evidence: сырые числа, на которых основан вывод (tx hash, shares,
            price, списанная комиссия, обе предсказанные величины).
        path: путь к файлу результата.

    Raises:
        ValueError: при basis == "unknown".
        FileExistsError: если результат уже зафиксирован.
    """
    if basis not in ("shares", "notional"):
        raise ValueError(f"basis must be 'shares' or 'notional', got {basis!r}")
    p = path or E2_RESULT_PATH
    if p.exists():
        raise FileExistsError(
            f"{p} уже существует. Удалите файл вручную, если действительно "
            "переопределяете результат Э2."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"basis": basis, "evidence": evidence}, indent=2, default=str),
        encoding="utf-8",
    )
    return p


def _fee_shares_basis(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    return shares * rate * price * (Decimal(1) - price)


def _fee_notional_basis(shares: Decimal, price: Decimal, rate: Decimal) -> Decimal:
    return (shares * price) * rate * price * (Decimal(1) - price)


def taker_fee(
    shares: Decimal | float | int,
    price: Decimal | float | str,
    vertical: Vertical | str,
    basis: FeeBasis | None = None,
) -> Decimal:
    """Точная комиссия тейкера в USD.

    Args:
        shares: число долей в сделке (не ноционал).
        price: цена исполнения в (0, 1).
        vertical: вертикаль рынка.
        basis: трактовка C. Если None -- берётся из результата Э2.

    Raises:
        FeeBasisUnresolved: если Э2 не завершён и basis не передан явно.
        ValueError: если цена вне (0, 1) или shares < 0.
    """
    b = basis or resolved_basis()
    if b == "unknown":
        raise FeeBasisUnresolved(
            "Трактовка C не определена (Э2 не завершён). Используйте "
            "fee_bracket() и несите bracket_width в отчёт, либо передайте "
            "basis= явно с пометкой 'предположение'."
        )
    s, p = Decimal(str(shares)), Decimal(str(price))
    if s < 0:
        raise ValueError("shares must be >= 0")
    if not (Decimal(0) < p < Decimal(1)):
        raise ValueError(f"price must be in (0,1), got {p}")
    rate = fee_rate(Vertical(vertical))
    fn = _fee_shares_basis if b == "shares" else _fee_notional_basis
    return fn(s, p, rate)


def maker_fee(*_args: object, **_kwargs: object) -> Decimal:
    """Комиссия мейкера. (а) Подтверждено: мейкеры не платят никогда.

    Функция существует, чтобы в расчётах не появлялся литерал 0 без ссылки
    на источник правила.
    """
    return Decimal(0)


def fee_bracket(
    shares: Decimal | float | int,
    price: Decimal | float | str,
    vertical: Vertical | str,
) -> FeeQuote:
    """Интервальная оценка комиссии тейкера при неизвестной трактовке C.

    Всегда безопасна для вызова: не требует результата Э2. Если Э2 завершён,
    low == high == point.
    """
    s, p = Decimal(str(shares)), Decimal(str(price))
    if not (Decimal(0) < p < Decimal(1)):
        raise ValueError(f"price must be in (0,1), got {p}")
    rate = fee_rate(Vertical(vertical))
    a = _fee_shares_basis(s, p, rate)
    b = _fee_notional_basis(s, p, rate)
    basis = resolved_basis()
    if basis == "shares":
        return FeeQuote(low=a, high=a, point=a, basis="shares")
    if basis == "notional":
        return FeeQuote(low=b, high=b, point=b, basis="notional")
    return FeeQuote(low=min(a, b), high=max(a, b), point=None, basis="unknown")
