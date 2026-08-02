"""ЗАДАЧА 3: гипотеза — несколько дельт одного токена в один ts_server_ms
на одной цене (size возвращается к прошлому значению) -> ложный dedup.

Проверка: ищем в tick_changes (там только ПРОШЕДШИЕ дельты) случаи
(token, ts_server_ms, price) с БОЛЕЕ ЧЕМ ОДНОЙ дельтой — это доказывает,
что сервер шлёт несколько изменений одного уровня в одну миллисекунду.
Если size при этом различается — коллизия dedup-ключа реальна."""
import duckdb

con = duckdb.connect("data/pm_step3.duckdb", read_only=True)

print("-- (token, ts_server_ms, price) с >1 дельтой в одну серверную мс --")
rows = con.execute(
    """
    SELECT token_id, ts_server_ms, price, COUNT(*) n,
           COUNT(DISTINCT size) n_sizes
    FROM tick_changes WHERE event_type='price_change'
    GROUP BY token_id, ts_server_ms, price
    HAVING COUNT(*) > 1
    """
).fetchall()
print(f"групп: {len(rows)}")
if rows:
    for r in rows[:10]:
        print("  ", r[0][:16], "ms", r[1], "price", r[2], "n", r[3], "n_sizes", r[4])

print("\n-- из них где n_sizes>1 (size прыгает туда-обратно) --")
multi = [r for r in rows if r[4] > 1]
print(f"n_sizes>1: {len(multi)} из {len(rows)}")

print("\n-- пример коллизии: та же цена, тот же ms, разные size подряд --")
for r in con.execute(
    """
    SELECT token_id, ts_server_ms, price, size, side, ts_recv_ms
    FROM tick_changes WHERE event_type='price_change'
      AND (token_id, ts_server_ms, price) IN (
        SELECT token_id, ts_server_ms, price
        FROM tick_changes WHERE event_type='price_change'
        GROUP BY token_id, ts_server_ms, price HAVING COUNT(DISTINCT size) > 1
      )
    ORDER BY token_id, ts_server_ms, price, ts_recv_ms LIMIT 20
    """
).fetchall():
    print("  ", r[0][:16], "ms", r[1], "p", r[2], "sz", r[3], r[4], "recv", r[5])

con.close()
