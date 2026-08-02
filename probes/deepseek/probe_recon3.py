"""ЗАДАЧА 3: разобрать recons_mismatch — dedup или реальные потери."""
import duckdb

con = duckdb.connect("data/pm_step3.duckdb", read_only=True)

print("tables:", [r[0] for r in con.execute(
    "SELECT table_name FROM information_schema.tables").fetchall()])

print("\n-- recon verdicts --")
for r in con.execute(
    "SELECT verdict, COUNT(*) FROM recon_checks GROUP BY verdict").fetchall():
    print(r)

n_total = con.execute("SELECT COUNT(*) FROM recon_checks").fetchone()[0]
n_mismatch = con.execute(
    "SELECT COUNT(*) FROM recon_checks WHERE verdict='mismatch'").fetchone()[0]
print(f"mismatch rate: {n_mismatch}/{n_total} = {100.0*n_mismatch/n_total:.2f}%")

print("\n-- recon rows per token (top 12 по числу mismatch) --")
for r in con.execute(
    """
    SELECT token_id, COUNT(*) n, SUM(CASE WHEN verdict='mismatch' THEN 1 ELSE 0 END) mism
    FROM recon_checks GROUP BY token_id
    ORDER BY mism DESC LIMIT 12
    """
).fetchall():
    print(r)

print("\n-- max_abs_diff_price / size распределение по mismatch --")
for r in con.execute(
    """
    SELECT n_levels_ours, n_levels_theirs,
           COUNT(*) n, AVG(max_abs_diff_price), AVG(max_abs_diff_size)
    FROM recon_checks WHERE verdict='mismatch'
    GROUP BY n_levels_ours, n_levels_theirs ORDER BY n DESC LIMIT 10
    """
).fetchall():
    print(r)

print("\n-- ts_recv_ms совпадения у recon (сколько было бы затерто старым ключом) --")
for r in con.execute(
    """
    WITH dups AS (
        SELECT token_id, ts_recv_ms, COUNT(*) c
        FROM recon_checks GROUP BY token_id, ts_recv_ms HAVING COUNT(*)>1
    )
    SELECT COUNT(*) groups, SUM(c) rows_affected FROM dups
    """
).fetchall():
    print(r)

con.close()
