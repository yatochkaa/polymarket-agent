"""ЗАДАЧА 3: сколько событий в tick_changes имеют РАЗНЫЕ значения,
но одинаковый dedup-ключ — т.е. сколько реальных событий задело бы dedup."""
import duckdb

con = duckdb.connect("data/pm_step3.duckdb", read_only=True)

print("-- кол-во tick_changes по event_type --")
for r in con.execute(
    "SELECT event_type, COUNT(*) FROM tick_changes GROUP BY event_type").fetchall():
    print(r)

print("\n-- дельты: кол-во групп по (token, ts_server, price, size, side, best_bid, best_ask) --")
n_total = con.execute(
    "SELECT COUNT(*) FROM tick_changes WHERE event_type='price_change'").fetchone()[0]
n_unique = con.execute(
    """
    SELECT COUNT(*) FROM (
      SELECT DISTINCT token_id, ts_server_ms, price, size, side, best_bid, best_ask
      FROM tick_changes WHERE event_type='price_change'
    )
    """).fetchone()[0]
print(f"total={n_total} unique={n_unique} dup_collisions={n_total-n_unique}")

print("\n-- ВАЖНО: одинаковые (token, ts_server_ms, price, size, side) но разные best_bid/best_ask --")
# best_bid/best_ask входят в dedup-ключ, поэтому дельта с теми же полями,
# но другим best_bid НЕ задедюплится. А вот одинаковые по всем полям — да.
n_dups = con.execute(
    """
    SELECT COUNT(*) FROM (
      SELECT token_id, ts_server_ms, price, size, side, best_bid, best_ask, COUNT(*) c
      FROM tick_changes WHERE event_type='price_change'
      GROUP BY token_id, ts_server_ms, price, size, side, best_bid, best_ask
      HAVING COUNT(*) > 1
    )
    """).fetchone()[0]
print(f"групп-дублей (по полному dedup-ключу, кроме seq): {n_dups}")

print("\n-- живые примеры дублей: один токен, (ts, price, size) повторяется --")
for r in con.execute(
    """
    SELECT token_id, ts_server_ms, price, size, side, best_bid, best_ask, COUNT(*) c
    FROM tick_changes WHERE event_type='price_change'
    GROUP BY token_id, ts_server_ms, price, size, side, best_bid, best_ask
    HAVING COUNT(*) > 1 ORDER BY c DESC LIMIT 8
    """).fetchall():
    print(r)

con.close()
