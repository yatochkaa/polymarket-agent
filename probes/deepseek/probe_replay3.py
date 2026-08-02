"""ЗАДАЧА 3: детерминирующий тест на одном токене с высоким mismatch.

Берём токен с max mismatch. Воспроизводим его поток из book_snapshots
(server book) + tick_changes (price_change). Симулируем LiveBook:
- вариант A: применять ВСЕ дельты (без dedup)
- вариант B: применять дельты с dedup (ключ как в ws_collector, окно MAX_DEDUP)
Сравниваем оба варианта с серверным book. Какой из них даёт mismatch —
тот и является причиной.

ВАЖНО: tick_changes уже без dedup-скоков (они отброшены коллектором),
поэтому вариант A здесь не совсем "без dedup", а "с теми дельтами, что
выжили". Зато можно оценить: достаточно ли выживших дельт для match.
Если даже по всем выжившим дельтам книга не сходится — это реальные
потери WS, а не dedup."""
import duckdb

con = duckdb.connect("data/pm_step3.duckdb", read_only=True)

token = "86909537651481288230229530743671630905703168715996646421242040547959053216258"

print("== токен:", token[:20], "==")
print("-- серверные book (последние 8) --")
books = con.execute(
    f"""
    SELECT ts_server_ms, ts_recv_ms, seq, bids, asks
    FROM book_snapshots
    WHERE token_id='{token}' AND source='ws'
      AND CAST(best_bid AS VARCHAR) IS NOT NULL
    ORDER BY seq DESC LIMIT 8
    """
).fetchall()
# книга содержит уровни — надо развернуть. Вместо этого проверим плотность.
print("  count ws-book:", con.execute(
    f"SELECT COUNT(*) FROM book_snapshots WHERE token_id='{token}' AND source='ws'"
).fetchone()[0])
print("  count deltas:", con.execute(
    f"SELECT COUNT(*) FROM tick_changes WHERE token_id='{token}' AND event_type='price_change'"
).fetchone()[0])
print("  recon:", con.execute(
    f"SELECT verdict, COUNT(*) FROM recon_checks WHERE token_id='{token}' GROUP BY verdict"
).fetchall())

print("\n-- Сколько серверных book у токена вообще (по event_type='book') --")
print("  ", con.execute(
    f"SELECT COUNT(*) FROM tick_changes WHERE token_id='{token}' AND event_type='book'"
).fetchone()[0])

print("\n-- Сколько дельт в секунду (recv) — бурстность --")
for r in con.execute(
    f"""
    SELECT ts_recv_ms // 1000 sec, COUNT(*) n
    FROM tick_changes WHERE token_id='{token}' AND event_type='price_change'
    GROUP BY sec ORDER BY n DESC LIMIT 5
    """
).fetchall():
    print("  ", r)

con.close()
