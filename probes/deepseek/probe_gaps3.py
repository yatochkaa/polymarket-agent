"""ЗАДАЧА 3: gap_intervals и распределение mismatch по времени."""
import duckdb

con = duckdb.connect("data/pm_step3.duckdb", read_only=True)

print("-- gap_intervals по reason --")
for r in con.execute(
    "SELECT reason, COUNT(*) FROM gap_intervals GROUP BY reason"
).fetchall():
    print("  ", r)

print("\n-- gap_intervals: диапазоны времени, длительность --")
for r in con.execute(
    """
    SELECT reason, COUNT(*), MIN(start_ms), MAX(end_ms)
    FROM gap_intervals GROUP BY reason
    """
).fetchall():
    print("  ", r[0], "n", r[1], "start", r[2], "end", r[3])

print("\n-- время первых 20 recon mismatch (в секундах от старта сессии) --")
sess = con.execute("SELECT MIN(started_ms), MAX(COALESCE(ended_ms, started_ms)) FROM collector_sessions").fetchone()
print("  сессия:", sess)
if sess and sess[0]:
    for r in con.execute(
        """
        SELECT ts_recv_ms, token_id FROM recon_checks
        WHERE verdict='mismatch' ORDER BY ts_recv_ms LIMIT 20
        """
    ).fetchall():
        print("   t+", (r[0]-sess[0])/1000.0, "s", r[1][:12])

print("\n-- mismatch по 30-сек бакетам (сколько секунд сессии) --")
for r in con.execute(
    """
    SELECT (ts_recv_ms - (SELECT MIN(started_ms) FROM collector_sessions))/30000 AS bucket,
           COUNT(*) n
    FROM recon_checks WHERE verdict='mismatch'
    GROUP BY bucket ORDER BY bucket
    """
).fetchall():
    print("   bucket", r[0], "->", r[1])

print("\n-- макс разрыв ts_recv_ms у recon (пропуски) --")
for r in con.execute(
    """
    SELECT token_id, ts_recv_ms, seq, n_levels_ours, n_levels_theirs, max_abs_diff_price, max_abs_diff_size, verdict
    FROM recon_checks WHERE verdict='mismatch'
    ORDER BY max_abs_diff_size DESC LIMIT 8
    """
).fetchall():
    print("  ", r[0][:12], "recv", r[1], "seq", r[2], "lv", r[3], r[4], "dp", round(r[5],4), "ds", round(r[6],1), r[7])

con.close()
