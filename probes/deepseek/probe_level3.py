"""ЗАДАЧА 3: для одного mismatch найти конкретный уровень, где расходится
размер, и проверить, была ли дельта по этой цене в предшествующие 500 мс.
Если дельты не было -> потеряно сообщение. Если была, но книга не в ней ->
порядок/дубль."""
import duckdb

con = duckdb.connect("data/probe_dedup_off.duckdb", read_only=True)

# Возьмём токен и первый mismatch с ds>0
token = con.execute(
    """
    SELECT token_id FROM recon_checks
    WHERE verdict='mismatch' AND max_abs_diff_size>0
    ORDER BY ts_recv_ms LIMIT 1
    """
).fetchone()[0]
row = con.execute(
    f"""
    SELECT ts_recv_ms, n_levels_ours, n_levels_theirs, max_abs_diff_size
    FROM recon_checks WHERE token_id='{token}' AND verdict='mismatch' AND max_abs_diff_size>0
    ORDER BY ts_recv_ms LIMIT 1
    """
).fetchone()
recv_ms = row[0]
print(f"токен {token[:14]} recon@{recv_ms} ds={row[3]}", flush=True)

# Найдём book_snapshots до этого recon: наша книга (из дельт) и серверный снимок.
# Проще: восстановим оба состояния напрямую из сохранённых снимков.
snap = con.execute(
    f"""
    SELECT seq, ts_recv_ms FROM book_snapshots
    WHERE token_id='{token}' AND ts_recv_ms <= {recv_ms}
    ORDER BY ts_recv_ms DESC LIMIT 1
    """
).fetchone()
print("последний book_snapshots до recon:", snap, flush=True)

# Дельты за 500 мс до recon
deltas = con.execute(
    f"""
    SELECT ts_recv_ms, price, size, side
    FROM tick_changes WHERE token_id='{token}' AND event_type='price_change'
      AND ts_recv_ms > {recv_ms} - 500 AND ts_recv_ms <= {recv_ms}
    ORDER BY ts_recv_ms
    """
).fetchall()
print(f"дельт за 500мс до recon: {len(deltas)}", flush=True)
for d in deltas[:15]:
    print("   ", d, flush=True)

# Найдём серверный снимок СРАЗУ после recon и его полную книгу
after = con.execute(
    f"""
    SELECT seq FROM book_snapshots
    WHERE token_id='{token}' AND ts_recv_ms >= {recv_ms} AND source='ws'
    ORDER BY ts_recv_ms LIMIT 1
    """
).fetchone()
print("первый ws-book после recon:", after, flush=True)

con.close()
