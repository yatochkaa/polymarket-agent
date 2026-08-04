import duckdb
con = duckdb.connect("data/pm.duckdb", read_only=True)

print("=== 1. verdicts ===")
for r in con.execute("SELECT verdict, count(*) FROM recon_checks GROUP BY verdict ORDER BY 2 DESC").fetchall():
    print(r)

print("=== 2. chto imenno rashoditsya v mismatch ===")
q = """
SELECT
  sum(CASE WHEN n_levels_ours <> n_levels_theirs THEN 1 ELSE 0 END) AS tolko_urovni,
  sum(CASE WHEN max_abs_diff_price > 0 THEN 1 ELSE 0 END) AS ceny,
  sum(CASE WHEN max_abs_diff_size > 0 THEN 1 ELSE 0 END) AS razmery,
  count(*) AS vsego
FROM recon_checks WHERE verdict = 'mismatch'
"""
print(con.execute(q).fetchall())

print("=== 3. velichina rashozhdeniya po cene ===")
q = """
SELECT round(max_abs_diff_price, 4) AS dp, count(*) AS n
FROM recon_checks WHERE verdict = 'mismatch'
GROUP BY 1 ORDER BY n DESC LIMIT 15
"""
for r in con.execute(q).fetchall():
    print(r)

print("=== 4. velichina rashozhdeniya po razmeru ===")
q = """
SELECT round(max_abs_diff_size, 2) AS ds, count(*) AS n
FROM recon_checks WHERE verdict = 'mismatch'
GROUP BY 1 ORDER BY n DESC LIMIT 15
"""
for r in con.execute(q).fetchall():
    print(r)

print("=== 5. raznica v chisle urovney (nashi minus ih) ===")
q = """
SELECT (n_levels_ours - n_levels_theirs) AS d, count(*) AS n
FROM recon_checks WHERE verdict = 'mismatch'
GROUP BY 1 ORDER BY n DESC LIMIT 15
"""
for r in con.execute(q).fetchall():
    print(r)

print("=== 6. ohvat: tokenov s mismatch / vsego tokenov ===")
q = """
SELECT
  count(DISTINCT CASE WHEN verdict = 'mismatch' THEN token_id END) AS tokenov_s_mismatch,
  count(DISTINCT token_id) AS tokenov_vsego
FROM recon_checks
"""
print(con.execute(q).fetchall())

print("=== 7. USTOYCHIVOST: chto v sleduyushchey sverke togo zhe tokena ===")
q = """
WITH s AS (
  SELECT token_id, ts_recv_ms, verdict,
         lead(verdict) OVER (PARTITION BY token_id ORDER BY ts_recv_ms) AS nxt
  FROM recon_checks WHERE verdict IN ('match','mismatch')
)
SELECT nxt AS sleduyushchaya, count(*) AS n
FROM s WHERE verdict = 'mismatch'
GROUP BY 1 ORDER BY n DESC
"""
for r in con.execute(q).fetchall():
    print(r)

print("=== 8. top-10 tokenov po chislu mismatch ===")
q = """
SELECT token_id,
       sum(CASE WHEN verdict = 'mismatch' THEN 1 ELSE 0 END) AS mism,
       sum(CASE WHEN verdict = 'match' THEN 1 ELSE 0 END) AS mat
FROM recon_checks WHERE verdict IN ('match','mismatch')
GROUP BY 1 ORDER BY mism DESC LIMIT 10
"""
for r in con.execute(q).fetchall():
    print(r)

con.close()
