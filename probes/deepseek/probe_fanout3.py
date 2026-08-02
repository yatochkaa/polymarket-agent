"""ЗАДАЧА 3: доказать server-side fan-out.

Сравнение двух dedup-OFF прогонов (один коннект vs два коннекта), одни и
те же 56 токенов, по ~2 мин. Если сервер шлёт события токена на ОБА
соединения (fan-out), суммарный объём событий в 2-conn прогоне должен быть
~2x от 1-conn (и +дубли внутри)."""
import duckdb

for path, label in (
    ("data/probe_dedup_off.duckdb", "2-conn"),   # сейчас это 2-conn прогон
):
    try:
        con = duckdb.connect(path, read_only=True)
    except Exception:
        continue
    n_events = con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0]
    n_tokens = con.execute("SELECT COUNT(DISTINCT token_id) FROM tick_changes").fetchone()[0]
    n_dups = con.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT token_id, ts_server_ms, price, size, side, best_bid, best_ask, COUNT(*) c
          FROM tick_changes WHERE event_type='price_change'
          GROUP BY token_id, ts_server_ms, price, size, side, best_bid, best_ask
          HAVING COUNT(*) > 1
        )
        """).fetchone()[0]
    print(f"{label}: events={n_events} tokens={n_tokens} dup_groups={n_dups}")
    con.close()

# 1-conn прогон был в data/probe_dedup_off_1conn? проверяем старые базы
import glob
for f in glob.glob("data/probe_*.duckdb"):
    try:
        con = duckdb.connect(f, read_only=True)
        n = con.execute("SELECT COUNT(*) FROM tick_changes").fetchone()[0]
        n_t = con.execute("SELECT COUNT(DISTINCT token_id) FROM tick_changes").fetchone()[0]
        n_b = con.execute("SELECT COUNT(*) FROM collector_sessions").fetchone()[0]
        conns = con.execute("SELECT markets_subscribed FROM collector_sessions LIMIT 1").fetchone()
        print(f"  {f}: events={n} tokens={n_t}")
        con.close()
    except Exception as exc:
        print(f"  {f}: {exc!r}")
