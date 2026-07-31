"""pm -- личный аналитический слой над Polymarket (разведка).

Модули:
- config: константы инфраструктуры и настройки из окружения;
- fees: ЕДИНСТВЕННЫЙ источник чисел комиссии;
- httpc: read-only HTTP клиент с журналом сырых ответов;
- markets: парсинг рынков Gamma;
- stats: кластерные SE, шринкаж, BH-FDR, решающее правило;
- store: артефакты (JSON/Parquet/DuckDB);
- experiments: Э1..Э4.
"""

__all__ = [
    "config",
    "fees",
    "httpc",
    "markets",
    "stats",
    "store",
]
__version__ = "0.1.0"
