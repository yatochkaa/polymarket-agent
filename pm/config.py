"""Конфигурация проекта. Все внешние константы собраны в одном месте
и промаркированы по статусу знания.

(а) ПОДТВЕРЖДЕНО вводными проекта:
    - CLOB V2 с 28.04.2026, Exchange 0xE1111..., NegRisk 0xe2222...
    - коллатерал pUSD
    - /orderbook-history мёртв с 20.02.2026
(в) ПРЕДПОЛОЖЕНИЕ (проверяется в probe.py preflight):
    - базовые URL хостов CLOB / Gamma / Data API и имена параметров
    - адрес pUSD и его decimals
    - топики событий OrderFilled
Любое значение с пометкой (в) обязано либо подтвердиться preflight-проверкой,
либо быть переопределено через .env до запуска экспериментов.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# --- (а) Ончейн-константы -------------------------------------------------
EXCHANGE_V2: Final[str] = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2: Final[str] = "0xe2222d279d744050d28e00520010520000310F59"
CLOB_V2_LIVE_FROM: Final[str] = "2026-04-28"
ORDERBOOK_HISTORY_DEAD_FROM: Final[str] = "2026-02-20"
COLLATERAL_SYMBOL: Final[str] = "pUSD"

# --- (в) Сетевые хосты ----------------------------------------------------
DEFAULT_CLOB_HOST: Final[str] = "https://clob.polymarket.com"
DEFAULT_GAMMA_HOST: Final[str] = "https://gamma-api.polymarket.com"
DEFAULT_DATA_HOST: Final[str] = "https://data-api.polymarket.com"
DEFAULT_RPC_URL: Final[str] = "https://polygon-rpc.com"

# --- Методологические пороги, зафиксированные ДО сбора данных -------
# Менять эти числа после начала эксперимента запрещено PREREGISTRATION.md.
E1_SIGMA_BOOK_MAX: Final[float] = 0.005
E1_DRIFT_TRADE_MIN: Final[float] = 0.02
WIDE_SPREAD_THRESHOLD: Final[float] = 0.10  # (а) выше него отображается last trade
DECISION_K: Final[float] = 2.5  # коэффициент в критерии GO
FDR_Q: Final[float] = 0.10  # BH-FDR уровень
MIN_EVENTS_PER_TRADER: Final[int] = 30  # гейт включения трейдера в ранжирование


class Settings(BaseSettings):
    """Настройки из окружения / .env.

    Ключи API не нужны ни для Э1, ни для Э4 (только чтение публичных
    эндпоинтов). Э2 нужен RPC. Э3 -- единственный блок, требующий ключей.
    """

    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="PM_", extra="ignore"
    )

    clob_host: str = DEFAULT_CLOB_HOST
    gamma_host: str = DEFAULT_GAMMA_HOST
    data_host: str = DEFAULT_DATA_HOST
    rpc_url: str = DEFAULT_RPC_URL

    # Креды только для Э3 (запись). Отсутствие -- норма.
    private_key: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    funder_address: str | None = None

    # Собственные адреса: всегда исключаются из любой аналитики.
    self_addresses: list[str] = Field(default_factory=list)

    data_dir: Path = Path("data")
    request_timeout_s: float = 20.0
    max_retries: int = 4
    rate_limit_rps: float = 4.0

    @field_validator("self_addresses", mode="before")
    @classmethod
    def _split_addresses(cls, v: object) -> object:
        """Позволяет задать PM_SELF_ADDRESSES=0xa,0xb одной строкой."""
        if isinstance(v, str):
            return [x.strip().lower() for x in v.split(",") if x.strip()]
        if isinstance(v, list):
            return [str(x).strip().lower() for x in v]
        return v

    def is_self(self, address: str | None) -> bool:
        """True, если адрес принадлежит нам (self-exclusion list)."""
        if not address:
            return False
        return address.strip().lower() in set(self.self_addresses)


def load_settings() -> Settings:
    """Создаёт Settings и гарантирует существование data_dir."""
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s
