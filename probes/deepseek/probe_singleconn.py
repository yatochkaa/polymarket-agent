"""ЗАДАЧА 3: один коннект, dedup ON (боевая конфигурация), 70 токенов,
2 мин. Проверяем: mismatch есть ли и какова природа (same-ms race)."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\awf\Desktop\test")

import src.collect.ws_collector as wsc


async def main() -> None:
    db = Path("data/probe_single_70.duckdb")
    if db.exists():
        db.unlink()
    exit_code = await wsc.run(
        minutes=2.0,
        db_path=db,
        export_root=Path("data/probe_single_70_export"),
        drop_rate=0.0,
        n_conns=1,
        vertical="crypto",
    )
    print(f"exit_code={exit_code}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
