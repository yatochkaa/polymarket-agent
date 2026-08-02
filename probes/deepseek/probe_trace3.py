"""ЗАДАЧА 3: трассировка одного токена с высоким mismatch на 2-conn прогоне.

Ищем, сколько дельт между двумя book-событиями, и проверяем гипотезу:
в 2-conn прогоне события токена приходят ДВАЖДЫ (с обоих соединений)
или ТЕРЯЮТСЯ. Сравниваем с single-conn прогоном по тому же токену."""
import duckdb

con = duckdb.connect("data/probe_dedup_off.duckdb", read_only=True)

token = "67511303592803977725644790922501802441542445773322562685308289194859184201544"
# попробуем найти точный токен по префиксу
row = con.execute(
    "SELECT DISTINCT token_id FROM recon_checks WHERE token_id LIKE '675113035928%' LIMIT 1"
).fetchone()
if row:
    token = row[0]
    print("токен:", token)
else:
    print("не найден префикс")
    con.close()
    raise SystemExit

print("\n-- первые 15 событий токена (в порядке приёма) --")
for r in con.execute(
    f"""
    SELECT ts_recv_ms, event_type, price, size, side, best_bid, best_ask
    FROM tick_changes WHERE token_id='{token}'
    ORDER BY ts_recv_ms LIMIT 15
    """
).fetchall():
    print("   recv", r[0], r[1], "p", r[2], "sz", r[3], r[4], "bb", r[5], "ba", r[6])

print("\n-- recon-строки токена (первые 12) --")
for r in con.execute(
    f"""
    SELECT ts_recv_ms, n_levels_ours, n_levels_theirs, max_abs_diff_price, max_abs_diff_size, verdict
    FROM recon_checks WHERE token_id='{token}'
    ORDER BY ts_recv_ms LIMIT 12
    """
).fetchall():
    print("   recv", r[0], "lv", r[1], r[2], "dp", round(r[3],4), "ds", round(r[4],1), r[5])

print("\n-- сколько book vs delta у токена --")
for r in con.execute(
    f"""
    SELECT event_type, COUNT(*) FROM tick_changes WHERE token_id='{token}' GROUP BY event_type
    """
).fetchall():
    print("   ", r)

# гипотеза двойного прихода: одинаковый (ts_server, price, size) повторяется?
print("\n-- дубли (ts_server, price, size, side) у токена (признак двойного прихода) --")
for r in con.execute(
    f"""
    SELECT ts_server_ms, price, size, side, COUNT(*) c
    FROM tick_changes WHERE token_id='{token}' AND event_type='price_change'
    GROUP BY ts_server_ms, price, size, side HAVING COUNT(*) > 1 ORDER BY c DESC LIMIT 8
    """
).fetchall():
    print("   ts", r[0], "p", r[1], "sz", r[2], r[3], "x", r[4])

con.close()
