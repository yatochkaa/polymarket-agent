# Задание 04 — Этап 1: свой сборщик снимков стакана (гейт G3)

Сессия без памяти. Кредитов мало. Цель — собственные снимки стакана,
потому что чужая история стакана мёртва с 20.02.2026.

## ВАЖНО — оболочка PowerShell на Windows

Все команды в этом файле записаны в bash-стиле для читаемости.
Рабочая среда — PowerShell на Windows, где `test`, `grep`, `sed`, `tee`,
`cat`, `head`, `ls` НЕ СУЩЕСТВУЮТ. Переводи по таблице:

| bash | PowerShell |
|---|---|
| `test -f X && echo OK` | `if (Test-Path X) { "OK" }` |
| `grep -n "P" X` | `Select-String -Pattern "P" -Path X` |
| `grep -q "P" X && echo OK` | `if (Select-String -Pattern "P" -Path X -Quiet) { "OK" }` |
| `grep -c "" X` | `(Get-Content X).Count` |
| `grep -rn "P" pm/ --include=*.py` | `Get-ChildItem pm -Recurse -Filter *.py \| Select-String -Pattern "P"` |
| `sed -n '30,50p' X` | `Get-Content X \| Select-Object -Skip 29 -First 21` |
| `cmd 2>&1 \| tee f.log` | `cmd 2>&1 \| Tee-Object -FilePath f.log` |
| `ls -la data/` | `Get-ChildItem data` |
| `head -1 X` | `Get-Content X -TotalCount 1` |
| `cat X` | `Get-Content X` |
| `mkdir -p data logs` | `New-Item -ItemType Directory -Force data, logs` |

Если команда не отработала — НЕ подбирай альтернативу молча.
Запиши точную команду и текст ошибки в `PROGRESS.md`.

## 0. Прочитай первыми (и только их)

1. `PREREGISTRATION.md`
2. `PROBE_RESULTS.md` — статус G1 и G2
3. `pm/config.py`
4. `pm/httpc.py`
5. `pm/store.py` — особенно мёртвый DDL в районе строк 56–80
6. `pm/markets.py`
7. `requirements.txt`

## 1. Уже известно — НЕ перепроверяй

- `/orderbook-history` молча отдаёт пустоту с 20.02.2026.
  Поэтому гейт G3 закрывается только своими данными.
- DDL в `pm/store.py` около строк 56–80 мёртвый: `connect()`
  никогда не вызывается. Это не баг схемы, а отсутствие вызова.
- Лимиты: `clob/book` 1500/10s; Gamma `/markets` 300/10s;
  Cloudflare замедляет, а не отклоняет.
- Стек зафиксирован: Python 3.12, httpx, websockets, duckdb, pyarrow,
  streamlit, python-telegram-bot, pydantic, systemd.
  ЗАПРЕЩЕНО: Docker Compose, Kafka, Kubernetes, Airflow,
  микросервисы, Postgres.
- `pydantic_settings` локально не установлен.
- Пагинация Gamma реализована правильно, не трогать.
- Залог — pUSD.
- Папка `src/` ещё не существует, её создаёт это задание.

## 2. Шаги

### Шаг 1. Сначала `SCHEMAS.md`, потом код

Создай `SCHEMAS.md` ДО написания сборщика. Опиши таблицы:

**`book_snapshots`** — обязательные поля:
- `ts_recv_ms` — время приёма на нашей стороне, ms
- `book_age_ms` — возраст снимка относительно времени биржи, ms
- `seq` — последовательный номер обновления от источника
- `market_id`, `token_id`, `event_id` (матч), `best_bid`, `best_ask`,
  `bid_size`, `ask_size`, `spread`, `vwap_bid_100`, `vwap_ask_100`,
  `source` (`ws` / `rest_backfill`)

Типы фиксированы: `ts_recv_ms`, `book_age_ms`, `seq` — int64;
`best_bid`, `best_ask`, `bid_size`, `ask_size`, `spread`,
`vwap_bid_100`, `vwap_ask_100` — float64.

`vwap_bid_100` и `vwap_ask_100` — средняя цена исполнения при съедании
100 долей стакана. Это НЕ украшение: без них нельзя посчитать
спред-издержку в `cost` и копируемость в фильтре 6.
`mid` НЕ хранится: он выводится из bid/ask при чтении.
Глубина стакана в JSON НЕ хранится — parquet с JSON-колонкой раздувается
и не агрегируется в duckdb.

**`gap_intervals`** — пропуски как первоклассные данные:
- `market_id`, `token_id`, `seq_from`, `seq_to`, `ts_from_ms`, `ts_to_ms`,
  `reason` (`seq_jump` / `disconnect` / `process_restart`), `n_missing`

**`collector_sessions`**: `session_id`, `started_ms`, `ended_ms`,
`git_commit`, `markets_subscribed`, `exit_reason`.

**`markets_tracked`**: `market_id`, `token_id`, `event_id`, `vertical`,
`start_ms`, `end_ms`, `resolved`.

Для каждого поля укажи тип, единицу измерения и источник
(WS-сообщение, REST-ответ, вычисляется локально).

### Шаг 2. Подключить существующий `connect()`

```bash
sed -n '40,90p' pm/store.py
grep -rn "connect(" . --include=*.py
```

Используй существующий DDL, а не пиши второй слой хранения.
Если его схема противоречит `SCHEMAS.md` — расширь DDL, отрази
изменение в `SCHEMAS.md`, и отметь это в `PROGRESS.md`.

### Шаг 3. `src/collector/ws_collector.py`

Требования:
- Подписка на WS-канал стакана по списку активных теннисных рынков.
- Каждый снимок ОБЯЗАТЕЛЬНО пишет `ts_recv_ms`, `book_age_ms`, `seq`.
  Без этих трёх полей данные непригодны для CLV.
- При разрыве нумерации `seq` — запись в `gap_intervals` с
  `n_missing = seq_new - seq_prev - 1`.
- Переподключение с экспоненциальной задержкой;
  каждый разрыв тоже создаёт запись `gap_intervals`
  с `reason = disconnect`.
- Пропуски НЕ заполняются и НЕ интерполируются. Пропуск — это данные.
  Никогда не считать интервал внутри пропуска наблюдаемым событием.
- Хранение: parquet с разбиением по дате и `market_id`,
  сверху duckdb как движок запросов.
- Запуск как systemd-юнит; файл юнита положи в
  `deploy/pm-collector.service`. Устанавливать в систему не надо.
- Идемпотентность: повторный запуск не дублирует строки;
  ключ `market_id + token_id + seq`.

### Шаг 4. `src/collector/coverage.py`

Отчёт покрытия, запуск:

```bash
python -m src.collector.coverage --db data/pm.duckdb
```

Печатает: окно наблюдения, долю времени в пропусках по каждому
рынку, медиану / p90 / максимум `book_age_ms`, число рынков
с долей пропусков менее 5%.

### Шаг 5. Смок-прогон 15 минут

```bash
mkdir -p data logs
python -m src.collector.ws_collector --minutes 15 --vertical sports \
  > logs/collector_smoke.log 2>&1
python -m src.collector.coverage --db data/pm.duckdb
```

### Шаг 6. Тесты `tests/test_collector.py`

Минимум четыре случая:
1. Разрыв `seq` создаёт запись `gap_intervals`
   с корректным `n_missing`.
2. Повторная вставка того же `seq` не создаёт дубля.
3. `book_age_ms` вычисляется и никогда не NULL.
4. При разрыве соединения создаётся гэп с `reason = disconnect`,
   а не тихое продолжение ряда.

## 3. Запреты

- Не использовать `/orderbook-history` как источник истории.
- Не заполнять и не сглаживать пропуски.
- Не вводить Docker Compose, Kafka, Kubernetes, Airflow, Postgres,
  микросервисы.
- Не ставить пакеты вне `requirements.txt`.
- Не использовать `py-clob-client`.
- Не создавать `pm/broker.py`, не отправлять ордера.
- Не менять `PREREGISTRATION.md` и `PROBE_RESULTS.md`.
- Не делать `git add`, `git commit`, `git push`.
- Не читать `.env`.
- Не начинать этап 4 (CLV) в этой сессии.

## 4. Критерий завершения (проверяемые команды)

```bash
test -f SCHEMAS.md && echo OK_SCHEMAS
grep -c "ts_recv_ms\|book_age_ms\|seq\|gap_intervals" SCHEMAS.md
python -m unittest discover -s tests -q
python -m src.collector.coverage --db data/pm.duckdb
ls -la data/
git status --short
```

Ожидается: `OK_SCHEMAS`; все четыре имени присутствуют; тесты зелёные,
включая `tests/test_collector.py`; coverage печатает непустой отчёт
за 15 минут; в `data/` есть parquet-файлы и duckdb-база.

Гейт G3 считается закрытым только после длительного сбора
на реальных матчах, а не после смок-прогона. В `PROBE_RESULTS.md`
статус G3 менять НЕЛЬЗЯ — это решение владельца.

## 5. Завершение сессии

- В `PROGRESS.md`: дата, созданные файлы, результат смок-прогона
  (число снимков, число гэпов, медиана `book_age_ms`),
  полные команды запуска.
- В `DECISIONS_NEEDED.md`: сколько дней непрерывного сбора считать
  достаточными для G3; какая доля пропусков допустима;
  включать ли рынки с широким спредом.
- Коммит НЕ делать.
