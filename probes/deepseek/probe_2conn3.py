"""ЗАДАЧА 3: структура mismatch на 2-conn dedup-off прогоне."""
import duckdb

con = duckdb.connect("data/probe_dedup_off.duckdb", read_only=True)

print("-- recon verdicts --")
for r in con.execute(
    "SELECT verdict, COUNT(*) FROM recon_checks GROUP BY verdict").fetchall():
    print("  ", r)

print("\n-- структура mismatch: n_levels + diffs --")
for r in con.execute(
    """
    SELECT n_levels_ours, n_levels_theirs, COUNT(*) n,
           AVG(max_abs_diff_price) dp, AVG(max_abs_diff_size) ds
    FROM recon_checks WHERE verdict='mismatch'
    GROUP BY n_levels_ours, n_levels_theirs ORDER BY n DESC LIMIT 8
    """
).fetchall():
    print("   ours", r[0], "theirs", r[1], "n", r[2], "dp", round(r[3],4), "ds", round(r[4],1))

print("\n-- первый mismatch по времени (сек от старта) --")
sess = con.execute("SELECT MIN(started_ms) FROM collector_sessions").fetchone()[0]
for r in con.execute(
    "SELECT ts_recv_ms, token_id FROM recon_checks WHERE verdict='mismatch' ORDER BY ts_recv_ms LIMIT 10"
).fetchall():
    print("   t+%.3f" % ((r[0]-sess)/1000.0), r[1][:12])

print("\n-- сколько рекон-строк на токен (последние) --")
for r in con.execute(
    """
    SELECT token_id, COUNT(*) n, SUM(CASE WHEN verdict='mismatch' THEN 1 ELSE 0 END) mism
    FROM recon_checks GROUP BY token_id ORDER BY mism DESC LIMIT 6
    """
).fetchall():
    print("   ", r[0][:12], "n", r[1], "mism", r[2])

con.close()
