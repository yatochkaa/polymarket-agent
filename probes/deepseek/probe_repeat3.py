"""ЗАДАЧА 3: точный тест ложного dedup.

Механизм: в одну серверную мс один уровень обновляется несколько раз:
size идёт 100 -> 50 -> 100. Первая и третья дельта имеют ОДИНАКОВЫЙ
dedup-ключ (token, ts, price, size, side, best_bid, best_ask) -> третья
пропускается, книга застревает на 50, сервер на 100 -> mismatch.

Доказательство: внутри групп (token, ts_server_ms, price) с >1 дельтой
считаем группы, где какое-то значение size (и side, best_bid, best_ask)
повторяется >=2 раза. Это точное условие ложного dedup."""
import duckdb

con = duckdb.connect("data/pm_step3.duckdb", read_only=True)

print("-- группы (token,ts,price) с >1 дельтой: повтор размера/полного ключа --")
rows = con.execute(
    """
    WITH g AS (
      SELECT token_id, ts_server_ms, price,
             COUNT(*) n,
             COUNT(DISTINCT size) n_sizes,
             COUNT(DISTINCT size || '|' || side || '|' || best_bid || '|' || best_ask) n_full
      FROM tick_changes WHERE event_type='price_change'
      GROUP BY token_id, ts_server_ms, price
      HAVING COUNT(*) > 1
    )
    SELECT
      COUNT(*) n_groups,
      SUM(CASE WHEN n_sizes < n THEN 1 ELSE 0 END) n_repeat_size,
      SUM(CASE WHEN n_full < n THEN 1 ELSE 0 END) n_repeat_fullkey
    FROM g
    """
).fetchone()
print("  групп(ts,price)>1 дельты:", rows[0])
print("  групп с ПОВТОРОМ size:", rows[1])
print("  групп с повтором ПОЛНОГО ключа:", rows[2])

print("\n-- сколько всего delta-событий в этих группах (верхняя граница потерь) --")
rows2 = con.execute(
    """
    WITH g AS (
      SELECT token_id, ts_server_ms, price,
             COUNT(*) n,
             COUNT(DISTINCT size || '|' || side || '|' || best_bid || '|' || best_ask) n_full
      FROM tick_changes WHERE event_type='price_change'
      GROUP BY token_id, ts_server_ms, price
      HAVING COUNT(*) > 1 AND COUNT(DISTINCT size || '|' || side || '|' || best_bid || '|' || best_ask) < COUNT(*)
    )
    SELECT COUNT(*) n_groups, SUM(n - n_full) n_extra
    FROM g
    """
).fetchone()
print("  групп с ложным dedup:", rows2[0], "потерянных дельт (оценка):", rows2[1])

print("\n-- пример: живая последовательность в одной серверной мс --")
for r in con.execute(
    """
    SELECT token_id, ts_server_ms, price, size, side, best_bid, best_ask, ts_recv_ms
    FROM tick_changes WHERE event_type='price_change'
      AND (token_id, ts_server_ms, price) IN (
        SELECT token_id, ts_server_ms, price
        FROM tick_changes WHERE event_type='price_change'
        GROUP BY token_id, ts_server_ms, price
        HAVING COUNT(*) > 1
           AND COUNT(DISTINCT size || '|' || side || '|' || best_bid || '|' || best_ask) < COUNT(*)
      )
    ORDER BY token_id, ts_server_ms, price, ts_recv_ms LIMIT 25
    """
).fetchall():
    print("  ", r[0][:12], "ms", r[1], "p", r[2], "sz", r[3], r[4], "bb", r[5], "ba", r[6], "recv", r[7])

con.close()
