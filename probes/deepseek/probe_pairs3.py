"""ЗАДАЧА 3: дубли recon в 2-conn прогоне — один book приходит 2 раза.

book_snapshots и recon_checks связаны по (token_id, seq): recon вызывается
в _handle_book с тем же seq, что у book_snapshots. Группируем по
(token_id, ts_server_ms): сколько book-событий и recon для одного серверного
снимка."""
import duckdb

con = duckdb.connect("data/probe_dedup_off.duckdb", read_only=True)

print("-- группы (token, ts_server_ms): сколько раз пришёл один book --")
for r in con.execute(
    """
    SELECT token_id, ts_server_ms, COUNT(*) n
    FROM book_snapshots WHERE source='ws'
    GROUP BY token_id, ts_server_ms
    HAVING COUNT(*) > 1
    ORDER BY n DESC LIMIT 8
    """
).fetchall():
    print("  ", r[0][:12], "ts", r[1], "приходов", r[2])

tot = con.execute("SELECT COUNT(*) FROM book_snapshots WHERE source='ws'").fetchone()[0]
uniq = con.execute(
    "SELECT COUNT(*) FROM (SELECT DISTINCT token_id, ts_server_ms FROM book_snapshots WHERE source='ws')"
).fetchone()[0]
print(f"\nвсего ws-book строк: {tot}, уникальных (token,ts): {uniq}, кратность: {tot/uniq:.2f}")

print("\n-- recon: сколько строк на (token, ts_server_ms of book) --")
# каждый book вызывает ровно один recon. Если один book пришёл 2 раза ->
# 2 recon для одного ts_server_ms.
r_tot = con.execute("SELECT COUNT(*) FROM recon_checks WHERE verdict!='warmup'").fetchone()[0]
r_uniq = con.execute(
    """
    SELECT COUNT(*) FROM (
      SELECT DISTINCT bs.token_id, bs.ts_server_ms
      FROM recon_checks rc JOIN book_snapshots bs
        ON rc.token_id=bs.token_id AND rc.seq=bs.seq
    )
    """
).fetchone()[0]
print(f"recon строк (не warmup): {r_tot}, уникальных book-событий: {r_uniq}, кратность: {r_tot/r_uniq:.2f}")

print("\n-- пары вердиктов для одного book, пришедшего 2 раза --")
rows = con.execute(
    """
    SELECT bs.token_id, bs.ts_server_ms, rc.verdict
    FROM recon_checks rc JOIN book_snapshots bs
      ON rc.token_id=bs.token_id AND rc.seq=bs.seq
    ORDER BY bs.token_id, bs.ts_server_ms, bs.seq
    """
).fetchall()
from collections import Counter
pair_stats = Counter()
i = 0
while i < len(rows) - 1:
    r1, r2 = rows[i], rows[i + 1]
    if r1[0] == r2[0] and r1[1] == r2[1]:
        key = ("M" if r1[2] == "match" else "m") + ("M" if r2[2] == "match" else "m")
        pair_stats[key] += 1
        i += 2
    else:
        i += 1
print("  пары (один book 2x):", dict(pair_stats))

con.close()
