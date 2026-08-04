import duckdb
con = duckdb.connect("data/pm.duckdb", read_only=True)
print("--- kolonki recon_checks ---")
for r in con.execute("PRAGMA table_info(recon_checks)").fetchall():
    print(r[1], "|", r[2])
print("--- vsego strok ---")
print(con.execute("SELECT count(*) FROM recon_checks").fetchone())
print("--- pervye 15 strok ---")
for r in con.execute("SELECT * FROM recon_checks LIMIT 15").fetchall():
    print(r)
con.close()
