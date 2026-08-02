"""ЗАДАЧА 3: подтвердить same-ms race.

Гипотеза: сервер шлёт book и price_change с ОДНИМ ts_server_ms. Если
delta пришла ДО book в порядке приёма, LiveBook уже включает изменение,
а book (сгенерированный до дельты) — нет -> ложный mismatch.

Проверка для mismatch: существует ли дельта с ts_server_ms == ts_server
book-снимка, принятая ДО recon (в пределах пары ms) и с ценой, где книга
расходится? Если да — это race, а не потеря: следующая book-строка даст match.

Проще: для каждого mismatch проверяем, есть ли delta, принятая в окне
(recv - 10, recv] с ts_server == ts_server book. И смотрим на следующий
recon того же токена: если следующий = match сразу — расхождение было
временным (race), а не стойким (потеря).
"""
import duckdb

con = duckdb.connect("data/probe_single_70.duckdb", read_only=True)

print("-- для каждого mismatch: следующий recon того же токена --")
rows = con.execute(
    """
    SELECT rc.token_id, rc.ts_recv_ms, rc.seq, rc.max_abs_diff_size
    FROM recon_checks rc
    WHERE rc.verdict='mismatch' AND rc.max_abs_diff_size>0
    ORDER BY rc.ts_recv_ms
    """
).fetchall()

next_verdicts = {"match": 0, "mismatch": 0, "warmup": 0, "none": 0}
race_ev = {"race": 0, "no_race_delta": 0}
for tok, recv, seq, ds in rows:
    # следующий recon по seq для того же токена
    nxt = con.execute(
        f"""
        SELECT verdict FROM recon_checks
        WHERE token_id='{tok}' AND seq > {seq}
        ORDER BY seq LIMIT 1
        """
    ).fetchone()
    if nxt is None:
        next_verdicts["none"] += 1
    else:
        next_verdicts[nxt[0]] += 1
    # дельта с тем же ts_server что у book, принятая в окне (recv-10, recv]
    same_srv = con.execute(
        f"""
        SELECT COUNT(*) FROM tick_changes t JOIN book_snapshots b
          ON t.token_id=b.token_id AND b.seq={seq}
        WHERE t.token_id='{tok}' AND t.event_type='price_change'
          AND t.ts_server_ms = b.ts_server_ms
          AND t.ts_recv_ms BETWEEN {recv}-10 AND {recv}
        """
    ).fetchone()[0]
    if same_srv > 0:
        race_ev["race"] += 1
    else:
        race_ev["no_race_delta"] += 1

print("  следующий вердикт:", next_verdicts)
print("  с дельтой same-ts_server в окне 10мс (race):", race_ev)

con.close()
