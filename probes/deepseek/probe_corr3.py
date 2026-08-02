"""ЗАДАЧА 3: корреляция dedup-скоков и recon mismatch во времени.
dedup-скоки в базу не пишутся (только счётчик), поэтому реконструируем
по дельтам: если сервер шлёт price_change с одинаковыми полями в одну
секунду — это кандидат в dedup. Но реальный вопрос: сколько токенов
подписано дважды (market_id дублируется)."""
import duckdb

con = duckdb.connect("data/pm_step3.duckdb", read_only=True)

print("-- markets_tracked: дубли токенов (двойная подписка) --")
dups = con.execute(
    """
    SELECT token_id, COUNT(*) c
    FROM markets_tracked GROUP BY token_id HAVING COUNT(*) > 1
    """
).fetchall()
print(f"дублей token_id: {len(dups)}")
if dups:
    for r in dups[:5]:
        print("  ", r[0][:16], r[1])

print("\n-- уникальные vs всего в подписке сессии --")
print(con.execute("SELECT COUNT(*), COUNT(DISTINCT token_id) FROM markets_tracked").fetchone())
print(con.execute("SELECT market_id, COUNT(*) FROM markets_tracked WHERE market_id IS NOT NULL GROUP BY market_id HAVING COUNT(*)>1").fetchall())

print("\n-- сколько в секунду событий: возможны ли коллизии dedup-ключа в бурсте --")
for r in con.execute(
    """
    SELECT token_id, ts_recv_ms // 1000 sec, COUNT(*) n, COUNT(DISTINCT price) n_prices
    FROM tick_changes WHERE event_type='price_change'
    GROUP BY token_id, ts_recv_ms // 1000
    ORDER BY n DESC LIMIT 6
    """
).fetchall():
    print("  ", r[0][:16], "sec", r[1], "n", r[2], "distinct_prices", r[3])

print("\n-- типичный max_abs_diff_size по токенам с high mismatch --")
for r in con.execute(
    """
    SELECT token_id, COUNT(*) mism, AVG(max_abs_diff_size) avg_sz
    FROM recon_checks WHERE verdict='mismatch'
    GROUP BY token_id ORDER BY mism DESC LIMIT 5
    """
).fetchall():
    print("  ", r[0][:16], "mism", r[1], "avg_size_diff", round(r[2],1))

con.close()
