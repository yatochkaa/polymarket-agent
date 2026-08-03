# SCHEMAS.md

Подтверждённые формы эндпоинтов Polymarket (измерено probe, не догадки).
Каждая строка — ФАКТ с датой измерения. Не менять без повторного probe.

## 2026-07-31 (probe_e1_shapes.py)

### CLOB (https://clob.polymarket.com)

**GET /book?token_id=<TOKEN_ID>**
- Параметр только `token_id`. `market=`/`asset_id=` -> 400 `{"error":"Invalid token id"}`.
- Ответ dict: `market, asset_id, timestamp, hash, bids, asks, min_order_size, tick_size, neg_risk, last_trade_price`.
- `bids`/`asks` = списки `{"price": "0.001", "size": "3086838.08"}` — ЦЕНЫ И РАЗМЕРЫ СТРОКАМИ.
- best_bid = max(price в bids), best_ask = min(price в asks); mid = (best_bid+best_ask)/2.
- `last_trade_price` и `tick_size` тоже строки. => /book даёт mid И last_trade одним вызовом.
- ФАКТ (2026-08-02): `timestamp` — 13-значное целое, epoch MILLISECONDS (те же
  единицы, что у WS `price_change`); bids по возрастанию цены, asks по убыванию;
  книга может быть пустой/односторонней.

**GET /midpoint?token_id=<TOKEN_ID>** -> `{"mid": "0.0295"}` (строка).

**GET /price?token_id=<TOKEN_ID>&side=buy|sell** -> `{"price": "..."}` (строка).
- side=buy = best bid (0.029), side=sell = best ask (0.03); сходится с /midpoint 0.0295.

**GET /prices-history?market=<TOKEN_ID>&startTs=<sec>&endTs=<sec>&fidelity=<min>**
- ❗ `market` ЗДЕСЬ = TOKEN ID (asset id), НЕ conditionId.
- Время только `startTs`/`endTs` (unix секунды) или `interval`. `startTime/endTime` -> 400. `token_id` вместо `market` -> 400.
- Ответ: `{"history": [{"t": <sec>, "p": <float>}]}`.

### Data API (https://data-api.polymarket.com)

**GET /trades?market=<CONDITION_ID>&limit=<n>**
- ❗ Фильтрует ТОЛЬКО `market=<conditionId>`. `market=<tokenId>` -> пусто.
- ❗❗ `asset_id`, `conditionId`, `takerAssetId` СЕРВЕР МОЛЧА ИГНОРИРУЕТ — возвращает ГЛОБАЛЬНУЮ ленту
  (в probe вылез посторонний btc-updown). Тот же класс ловушки, что tag_slug у /markets.
- По токену фильтруем КЛИЕНТСКИ по полю `asset`.
- Запись: `proxyWallet, side (BUY/SELL), asset (tokenId), conditionId, size, price (float), timestamp (sec), outcome, outcomeIndex, slug, title`.
- Пагинация НЕ прощупана (только limit); порядок — по убыванию timestamp.

### Три разных смысла слова "market" (источник ошибок!)
- /book, /midpoint, /price: токен в `token_id`.
- /prices-history: токен в `market`.
- /trades: conditionId в `market`.

## 2026-08-02 — WS CLOB (wss://ws-subscriptions-clob.polymarket.com/ws/market)

ФАКТЫ захвата 184 сообщений за 120 c (см. `logs/_ws_analysis.txt`):
- Подписка: `{"type": "market", "assets_ids": [token_id, ...]}`.
- Сообщения: `book` (полный стакан), `price_change` (изменение уровней,
  `price_changes` — массив, обычно ПАРА: подписанный asset + комплемент),
  `last_trade_price`, `book_array` (стартовый снимок-список).
- Поля сообщения: `market` (0x-адрес рынка), `asset_id` (token_id, десятичное),
  `timestamp` (13-значное целое, epoch ms, есть у всех типов).
- Наличие `hash` зависит от типа (уточнение 2026-08-03, исправляет прежнее
  заявление «у всех типов»; источник — захват 2026-08-02, `logs/_ws_analysis.txt`):

  | тип сообщения | где лежит hash | подтверждено |
  |---|---|---|
  | `book` | на уровне сообщения (`payload["hash"]`) | да, `logs/_ws_analysis.txt:25` |
  | `price_change` | у каждого элемента `price_changes[i]["hash"]` | да, строки 32-34 |
  | `last_trade_price` | `hash` ОТСУТСТВУЕТ (0 из 2 в захвате); есть только `transaction_hash` | да, строки 28-29 |

  Ключ дедупа (решение владельца 2026-08-03, см. DECISIONS_NEEDED.md):
  `book` и `price_change` -> `(asset_id, hash)`; `last_trade_price` ->
  `(asset_id, transaction_hash, price, size)`; тип без обоих полей -> исключение.
- `price_changes[i]` несёт: `asset_id, price, size, side (BUY/SELL), hash,
  best_bid, best_ask` — лучшие цены прямо в сообщении.
- СЕРВЕРНОГО seq НЕТ (поля seq/sequence/seq_num не найдены ни в одном типе) —
  разрывы серверной нумерацией не детектируются. Детектор потерь —
  `recon_checks` (сравнение восстановленной книги с серверным снимком).

## 2026-08-02 — ЗАМОРОЖЕННАЯ СХЕМА КОЛЛЕКТОРА (этап 1, гейт G3)

Контракт коллектора и проверочного контура. Менять без повторного probe нельзя.

Единицы времени везде: **epoch миллисекунды, целое (int64)**.
`seq` — **ЛОКАЛЬНЫЙ счётчик приёмника, НЕ серверный**: серверного seq нет
(проверено). Для каждого token_id счётчик продолжается от `max(seq)` в базе
(идемпотентный рестарт) и инкрементируется на каждую вставленную строку.

Источники: `ws` — WS-сообщение; `rest` — REST /book; `calc` — вычисляется локально.

### book_snapshots

Снимки состояния книги: каждая строка — наблюдаемое состояние `token_id`
на момент `ts_recv_ms`. Это данные для CLV.

| колонка | тип | источник | смысл |
|---|---|---|---|
| ts_recv_ms | BIGINT NOT NULL | calc | время приёма на нашей стороне, ms |
| ts_server_ms | BIGINT NULL | ws/rest | серверная метка из сообщения, ms |
| token_id | TEXT NOT NULL | ws/rest | asset_id (исход) |
| best_bid | DOUBLE NULL | ws/rest | лучшая цена покупки |
| best_ask | DOUBLE NULL | ws/rest | лучшая цена продажи |
| bid_size | DOUBLE NULL | ws/rest | размер лучшего бида |
| ask_size | DOUBLE NULL | ws/rest | размер лучшего аска |
| spread | DOUBLE NULL | calc | best_ask − best_bid |
| vwap_bid_100 | DOUBLE NULL | calc | средняя цена съедания 100 долей бида |
| vwap_ask_100 | DOUBLE NULL | calc | средняя цена съедания 100 долей аска |
| book_age_ms | BIGINT NULL | calc | ts_recv_ms − ts_server_ms (NULL если нет метки) |
| seq | BIGINT NOT NULL | calc | локальный счётчик приёмника по token_id |
| source | TEXT NOT NULL | calc | `ws` или `rest_backfill` |

Естественный ключ: `(token_id, seq)`.
`mid` НЕ хранится (выводится из bid/ask при чтении). Глубина стакана НЕ хранится
(полные массивы уровней — в `tick_changes.raw`).

### tick_changes

Сырая лента: каждое `price_change`-событие и `last_trade_price` дословно,
плюс полные `book`-снимки для восстановления книги. Без интерпретации.

| колонка | тип | источник | смысл |
|---|---|---|---|
| ts_recv_ms | BIGINT NOT NULL | calc | время приёма, ms |
| ts_server_ms | BIGINT NULL | ws | серверная метка сообщения, ms |
| token_id | TEXT NOT NULL | ws | asset_id |
| event_type | TEXT NOT NULL | ws | `book` / `price_change` / `last_trade_price` |
| side | TEXT NULL | ws | `BUY` / `SELL` (для price_change/last_trade) |
| price | DOUBLE NULL | ws | цена уровня (price_change/last_trade) |
| size | DOUBLE NULL | ws | размер (price_change/last_trade) |
| best_bid | DOUBLE NULL | ws | из сообщения |
| best_ask | DOUBLE NULL | ws | из сообщения |
| raw | TEXT NOT NULL | ws | исходный JSON сообщения дословно |
| seq | BIGINT NOT NULL | calc | локальный счётчик приёмника по token_id |

Естественный ключ: `(token_id, seq)`.
Анти-дубль при двойной подписке: уникальный `hash` из сообщения
(цена/размер/транзакция) держится в ограниченном множестве на приёме.

### gap_intervals

Пропуски — первоклассные данные. Пропуск НЕ заполняется и НЕ интерполируется.

| колонка | тип | источник | смысл |
|---|---|---|---|
| token_id | TEXT NOT NULL | calc | какой исход затронут |
| start_ms | BIGINT NOT NULL | calc | начало интервала, ms |
| end_ms | BIGINT NOT NULL | calc | конец интервала, ms |
| reason | TEXT NOT NULL | calc | см. ниже |
| n_missing | BIGINT NULL | calc | для разрыва seq: seq_new − seq_prev − 1 |

`reason` ∈ {`time_gap`, `server_resync`, `disconnect`, `process_restart`}:
- `disconnect` — обрыв WS-соединения (пишется ВСЕГДА при обрыве);
- `process_restart` — запуск нового процесса при прошлых данных в базе;
- `server_resync` — сервер прислал полный снимок после переподключения;
- `time_gap` — мягкий флаг тишины (молчащий рынок НЕ обязан быть разрывом;
  порог — именованная константа, порог ещё не проверен на живых данных).

Естественный ключ: `(token_id, start_ms, end_ms, reason)`.
Смена рынка (рынок умер, 404 `No orderbook exists`) — НЕ разрыв: в
`gap_intervals` не пишется, фиксируется как `markets_tracked` + переподписка.

### recon_checks

Проверка целостности: сравнение восстановленной из дельт книги (наша) с
каждым полным серверным снимком `book`. Лучший детектор потерь.

| колонка | тип | источник | смысл |
|---|---|---|---|
| ts_recv_ms | BIGINT NOT NULL | calc | время приёма снимка, ms |
| token_id | TEXT NOT NULL | calc | asset_id |
| seq | BIGINT NOT NULL | calc | локальный seq приёмника (тот же, что у book_snapshots) |
| n_levels_ours | BIGINT NOT NULL | calc | число уровней в восстановленной книге |
| n_levels_theirs | BIGINT NOT NULL | calc | число уровней в серверном снимке |
| max_abs_diff_price | DOUBLE NOT NULL | calc | макс |best_ours − best_theirs| по обеим сторонам |
| max_abs_diff_size | DOUBLE NOT NULL | calc | макс |size_ours − size_theirs| на общих ценах |
| verdict | TEXT NOT NULL | calc | `warmup` / `match` / `mismatch` |

`warmup` — наша книга ещё не инициализирована (первый снимок после подписки/
ресинка); `match` — книги идентичны (n_levels равны, diff_price=0, diff_size=0);
`mismatch` — расхождение (потеря/дубль сообщений).

Естественный ключ: `(token_id, seq)`. Локальный seq уникален в рамках токена
(одно значение — на один `book`/`price_change`-снимок), поэтому сравнение
сохраняется всегда: два `book` одного токена в одну миллисекунду (совпал
`ts_recv_ms`) НЕ затирают друг друга — recon не теряет ни одной строки
(ЗАДАЧА 3: "throw away nothing").

### collector_sessions

| колонка | тип | источник | смысл |
|---|---|---|---|
| session_id | TEXT NOT NULL | calc | uuid сессии |
| started_ms | BIGINT NOT NULL | calc | старт, ms |
| ended_ms | BIGINT NULL | calc | завершение, ms |
| git_commit | TEXT NULL | calc | `git rev-parse HEAD` на старте |
| markets_subscribed | INTEGER NOT NULL | calc | число токенов в подписке на старте |
| exit_reason | TEXT NULL | calc | причина завершения (`user_stop`, `error`, ...) |

### conn_stats

Per-connection статистика одной сессии (приёмка мультисоединённого
транспорта, Задача 2, решение владельца 2026-08-03). Одна строка на
соединение; пишет коллектор в конце `run()` из счётчиков, которые велись
по `conn_id` по мере приёма. Позволяет отличить одно вставшее соединение
от двух работающих: без этого критерии C4 (молчание > 90 с) и C5
(поток сообщений) невычислимы — общие `stats`/`heartbeat` неразделимы
между соединениями.

| колонка | тип | источник | смысл |
|---|---|---|---|
| session_id | TEXT NOT NULL | calc | uuid сессии |
| conn_id | INTEGER NOT NULL | calc | номер соединения (0-based) |
| n_tokens | INTEGER NOT NULL | calc | число токенов в подписке этого соединения |
| messages | BIGINT NOT NULL | calc | принятых WS-сообщений на этом соединении |
| events | BIGINT NOT NULL | calc | разобранных событий (book/delta/trade) |
| recons | BIGINT NOT NULL | calc | сверок recon_checks |
| recons_mismatch | BIGINT NOT NULL | calc | из них с verdict=mismatch |
| max_silence_s | DOUBLE NOT NULL | calc | максимум тишины (без единого сообщения), с |
| n_silence_episodes | INTEGER NOT NULL | calc | эпизодов молчания дольше SILENCE_THRESHOLD_S |
| n_pings_fired | INTEGER NOT NULL | calc | раз простой между приёмами >= PING_INTERVAL_S |
| first_msg_ms | BIGINT NULL | calc | время первого сообщения (NULL = ни одного) |
| last_msg_ms | BIGINT NULL | calc | время последнего сообщения (NULL = ни одного) |

Естественный ключ: `(session_id, conn_id)`. `first_msg_ms`/`last_msg_ms`
равны NULL, когда соединение не приняло ни одного сообщения (сразу
встало) — это ДАННЫЕ для C4, а не пропуск: не заполнять forward fill.

### markets_tracked

| колонка | тип | источник | смысл |
|---|---|---|---|
| token_id | TEXT NOT NULL | gamma | asset_id |
| market_id | TEXT NULL | ws | 0x-market из сообщения (заполняется при первом сообщении) |
| event_id | TEXT NULL | gamma | слаг рынка up/down (5m-рынок = событие) |
| vertical | TEXT NULL | calc | `crypto` |
| start_ms | BIGINT NOT NULL | calc | начало отслеживания |
| end_ms | BIGINT NULL | calc | завершение отслеживания |
| resolved | BOOLEAN NULL | gamma | признак резолва |

### own_orders

Заглушка: пустая таблица (ордера не отправляются, `pm/broker.py` не создаётся).
Схема появится на этапе, где это понадобится.

## Кто пишет / кто читает

- Пишет: `src/collect/` (ws_collector, store). Проверочный контур `src/validate/`
  пишет в свой parquet (12 колонок, замороженная схема book_poller).
- Читает: `src/collect/coverage.py` (отчёт), `src/validate/compare.py`
  (сверка против pmdata по parquet-проекции 12 колонок),
  этап 4 (CLV): только `source='ws'`, никогда `rest_backfill` внутри разрыва.
- `conn_stats`: читает `probes/deepseek/probe_accept_conns.py` (приёмка
  Задачи 2) — критерии C2/C3/C4/C5 по прогонам 2conn/1conn/2conn/1conn.
