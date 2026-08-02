"""ЗАДАЧА 3 (decisive): 70 токенов crypto, 2 мин, один коннект.

Вариант с dedup ВЫКЛЮЧЕННЫМ (monkeypatch Collector._is_duplicate -> False).
Сравнить recons_mismatch с базовым прогоном (с dedup).
Пишет статистику и recon-сводку в stdout; базу - во временный файл."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\awf\Desktop\test")

import src.collect.ws_collector as wsc
from src.collect import ws_collector

orig_is_duplicate = ws_collector.Collector._is_duplicate


def no_dedup(self, key: str) -> bool:
    return False


async def main() -> None:
    db = Path("data/probe_dedup_off.duckdb")
    if db.exists():
        db.unlink()
    exit_code = await wsc.run(
        minutes=2.0,
        db_path=db,
        export_root=Path("data/probe_dedup_off_export"),
        drop_rate=0.0,
        n_conns=2,
        vertical="crypto",
    )
    print(f"exit_code={exit_code}", flush=True)


if __name__ == "__main__":
    # Без dedup
    ws_collector.Collector._is_duplicate = no_dedup
    asyncio.run(main())
