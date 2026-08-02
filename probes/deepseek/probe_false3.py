"""ЗАДАЧА 3: доказать ложный dedup.

Механизм: в одну серверную мс один уровень обновляется несколько раз
(100 -> 50 -> 100). Первая и третья дельта имеют ОДИНАКОВЫЙ dedup-ключ
(token, ts_server, price, size, side, best_bid, best_ask). Коллектор
применяет первую (size=100), затем вторую (size=50), затем ТРЕТЬЮ
пропускает как "дубль" — книга застревает на 50, сервер на 100 -> mismatch.

Доказательство: в tick_changes выжили только первые вхождения, поэтому
мы ищем токены, где в одну серверную мс на одну цену есть 2+ дельты с
РАЗНЫМИ size (это уже доказано: 15870 групп). Теперь докажем, что
частота таких групп коррелирует с частотой mismatch по токенам.
"""
import duckdb

con = duckdb.connect("data/pm_step3.duckdb", read_only=True)

# Для каждого токена: число серверных мс с 2+ дельтами на одну цену
# (потенциальные ложные dedup) vs число recon mismatch.
print("-- корреляция потенц.ложных-dedup мс vs recon mismatch по токенам --")
rows = con.execute(
    """
    WITH per_token AS (
      SELECT token_id,
        COUNT(DISTINCT ts_server_ms || '|' || price) AS coll_ms,
        COUNT(*) AS ndeltas
      FROM tick_changes WHERE event_type='price_change'
      GROUP BY token_id
    ),
    rec AS (
      SELECT token_id, SUM(CASE WHEN verdict='mismatch' THEN 1 ELSE 0 END) mism,
             COUNT(*) nrec
      FROM recon_checks GROUP BY token_id
    )
    SELECT pt.token_id, pt.coll_ms, pt.ndeltas, COALESCE(rc.mism,0) mism, COALESCE(rc.nrec,0) nrec
    FROM per_token pt LEFT JOIN rec rc USING(token_id)
    ORDER BY COALESCE(rc.mism,0) DESC LIMIT 15
    """
).fetchall()
for r in rows:
    rate = 100.0 * r[3] / r[4] if r[4] else 0.0
    print(f"  {r[0][:14]} coll_ms={r[1]:6d} deltas={r[2]:6d} mism={r[3]:5d} ({rate:.1f}%)")

print("\n-- среднее: есть ли связь coll_ms/ndeltas при высоком mismatch? --")
corr = con.execute(
    """
    WITH per_token AS (
      SELECT token_id,
        COUNT(DISTINCT ts_server_ms || '|' || price) AS coll_ms,
        COUNT(*) AS ndeltas
      FROM tick_changes WHERE event_type='price_change'
      GROUP BY token_id
    ),
    rec AS (
      SELECT token_id, SUM(CASE WHEN verdict='mismatch' THEN 1 ELSE 0 END) mism
      FROM recon_checks GROUP BY token_id
    )
    SELECT COALESCE(rc.mism,0)>=5 AS high_mism,
           AVG(pt.coll_ms*1.0/pt.ndeltas) AS coll_frac,
           COUNT(*) ntok
    FROM per_token pt LEFT JOIN rec rc USING(token_id)
    GROUP BY high_mism
    """
).fetchall()
for r in corr:
    print("  high_mism=", r[0], "avg_coll_frac=", round(r[1],4), "n_tok=", r[2])

con.close()
