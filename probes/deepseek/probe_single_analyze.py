"""ЗАДАЧА 3: анализ mismatch на single-conn прогоне (98 токенов).

Вопрос: mismatch вызван потерей дельты или race (book и delta в одну
серверную мс)? Если race: у mismatch-снимка есть delta с тем же
ts_server_ms, что и book, или delta сразу после book, которая чинит книгу.
"""
import duckdb

con = duckdb.connect("data/probe_single_70.duckdb", read_only=True)

print("-- вердикты --")
for r in con.execute("SELECT verdict, COUNT(*) FROM recon_checks GROUP BY verdict").fetchall():
    print("  ", r)

print("\n-- структура mismatch --")
for r in con.execute(
    """
    SELECT n_levels_ours, n_levels_theirs, COUNT(*) n,
           AVG(max_abs_diff_price) dp, AVG(max_abs_diff_size) ds
    FROM recon_checks WHERE verdict='mismatch'
    GROUP BY n_levels_ours, n_levels_theirs ORDER BY n DESC LIMIT 6
    """
).fetchall():
    print("   ours", r[0], "theirs", r[1], "n", r[2], "dp", round(r[3],4), "ds", round(r[4],1))

# Race-тест: для mismatch, есть ли дельта в том же ts_server_ms, что book?
# Сопоставим book (recon) с дельтами за окно ±50мс
print("\n-- тест race: дельты вокруг mismatch-снимка (окно 100мс) --")
mismatches = con.execute(
    """
    SELECT rc.token_id, rc.ts_recv_ms, bs.ts_server_ms, rc.max_abs_diff_size
    FROM recon_checks rc JOIN book_snapshots bs
      ON rc.token_id=bs.token_id AND rc.seq=bs.seq
    WHERE rc.verdict='mismatch' AND rc.max_abs_diff_size>0
    ORDER BY rc.ts_recv_ms LIMIT 10
    """
).fetchall()
for m in mismatches:
    tok, recv, ts_srv, ds = m
    nearby = con.execute(
        f"""
        SELECT COUNT(*) FROM tick_changes
        WHERE token_id='{tok}' AND event_type='price_change'
          AND ts_recv_ms BETWEEN {recv}-100 AND {recv}+100
        """
    ).fetchone()[0]
    same_srv = con.execute(
        f"""
        SELECT COUNT(*) FROM tick_changes
        WHERE token_id='{tok}' AND event_type='price_change'
          AND ts_server_ms = {ts_srv}
        """
    ).fetchone()[0]
    print(f"   token {tok[:12]} book_srv={ts_srv} ds={ds} дельт±100ms={nearby} дельт_same_srv={same_srv}")

print("\n-- тест: сколько дельт имеют ts_server_ms совпадающий с book ts_server (в целом) --")
for r in con.execute(
    """
    WITH bk AS (
      SELECT token_id, ts_server_ms FROM book_snapshots WHERE source='ws'
    )
    SELECT COUNT(*) n_same FROM tick_changes t JOIN bk
      ON t.token_id=bk.token_id AND t.ts_server_ms=bk.ts_server_ms
      WHERE t.event_type='price_change'
    """
).fetchall():
    print("   дельт с ts_server == book ts_server:", r[0])

con.close()
