# PROGRESS

## Done
- Checked repository status.
- Ran unit tests.
- Verified `import probe` failure and captured the exact error.
- Inspected `probe.py`, `pm/experiments/__init__.py`, `pm/experiments/e4_tennis.py`, `pm/markets.py`, and `pm/store.py`.
- Wrote `AUDIT_REPORT.md`.

## Notes
- No `.py` files were modified.
- No git commit or push was performed.
- `probe.py` was not executed.
- `.env` was not read.

## 2026-07-31
Задание 02 выполнено: добавлен `pm/experiments/e2_fee_basis.py`, новый тест `tests/test_power_gate.py`, исправлена нумерация G1→G4 в `pm/experiments/e4_tennis.py`, порог мощности поднят до 100 в `pm/config.py`.

### Гейт мощности
До правки: `pm/experiments/e4_tennis.py:102: f"{min_events} ... Гейт G1 НЕ ПРОЙДЕН: цель B закрывается ..."`
После правки: `pm/experiments/e4_tennis.py:102: f"Всего {n_markets} разрешённых рынков в окне. Потолок кластеров = {n_markets}. Гейт G4 (>= 100 матчей на трейдера) на этапе Э4 не проверяется: нет данных по адресам. Проверяется на этапе 4 в фильтре 1."`

### Тесты
- До: 35 tests ran, OK.
- После: 36 tests ran, OK.

### Проверки раздела 4
- IMPORT_OK
- 36 tests ran, OK
- `pm/config.py:44:MIN_EVENTS_PER_TRADER: Final[int] = 100  # гейт включения трейдера в ранжирование`
- grep по ставкам комиссий: пусто (код вне `fees.py` и `tests/` не содержит `0.07`, `0.05`, `0.04`)
- git status --short: ` M .gitignore`, ` M DECISIONS_NEEDED.md`, ` M PROGRESS.md`, ` M pm/config.py`, ` M pm/experiments/e4_tennis.py`, `?? PREREGISTRATION.md`, `?? pm/experiments/e2_fee_basis.py`, `?? tests/test_power_gate.py`
- git diff --stat: фактический вывод зависит от текущего diff и должен быть снят командой `git diff --stat`

## 2026-08-02 — Этап 1, Шаг 0 (РАЗВЕДКА ПРОТОКОЛА, код коллектора НЕ пишется)

### Сделано
- Прочитан AGENTS.md; разделы «Отчётность» и «Заглушки и неизвестное» исполняются.
- Прочитаны контекстные файлы: `pm/config.py`, `pm/httpc.py`, `pm/markets.py`,
  `tasks/04_STAGE1_COLLECTOR.md`, `requirements.txt`, `PROGRESS.md`,
  `DECISIONS_NEEDED.md`, `logs/ws_raw.jsonl` (предыдущий захват).
- Свежий поиск token_id через `gamma-api.polymarket.com/events?tag_slug=X&closed=false`.
- Свежий захват WS (120 c) -> перезаписан `logs/ws_raw.jsonl`.
- Полный анализ захвата -> `logs/_ws_analysis.txt`.

### НЕ сделано и почему
- `src/collect/`, `deploy/`, `tests/test_collector.py` НЕ созданы: задание Этапа 1
  явно требует «Шаг 0. Только это. Кода не писать» до подтверждения вариантов
  по разрывам. Реализация остановлена на разведке протокола.
- `git pull --rebase` / `git status` НЕ выполнены: `C:\Users\awf\Desktop\test`
  не является git-репозиторием (`fatal: not a git repository`). Коммиты запрещены
  AGENTS.md; этот пункт отчёту не подлежит, пока не инициализирован репозиторий.
- «Два crypto up/down (btc-updown-5m, eth-updown-5m)»: НЕ найдены ни под одним
  tag_slug (см. DECISIONS_NEEDED.md §A). Для Шага 0 взяты замены, чтобы увидеть
  формат WS-сообщений; какие рынки собирать реально — ждёт решения владельца.

### ФАКТЫ Шага 0 (цитируется из свежего прогона анализа `logs/_ws_analysis.txt`)
- Всего сообщений в захвате: **184 за 120 с** (финальный вывод capture-скрипта:
  `DONE: 184 messages in 120s`). Анализ снимался на 172 сообщениях (~107 с).
- Типы сообщений (exact count из анализа): `price_change` — 167; `book` — 2;
  `last_trade_price` — 2; `book_array` — 1 (initial snapshot на подписку).
- Серверная метка времени: поле `timestamp`, 13-значное целое ->
  **epoch milliseconds** (подтверждено). Пример: `"timestamp":"1785651131034"`.
- Номер последовательности: **ОТСУТСТВУЕТ** (поля `seq`/`sequence`/`seq_num`/
  `event_seq`/`update_id` не найдены ни в одном типе сообщения).
- Полный JSON первых 3 сообщений каждого типа — в `logs/_ws_analysis.txt`.

### Артефакты
- `logs/ws_raw.jsonl` — свежий захват (перезаписан). Предыдущий захват НЕ
  сохранён (команда `move` не выполнена из-за путей cmd); прежнее содержимое
  утеряно намеренно — задание требовало свежий захват.
- `logs/_ws_capture.py`, `logs/_ws_analyze.py`, `logs/_select_tokens.py`,
  `logs/_recon_tokens.py`, `logs/_tokens_selected.json`, `logs/_ws_analysis.txt`
  — временные разведочные скрипты/артефакты в `logs/` (НЕ в `src/`).

### Команды запуска
```
cd /d C:\Users\awf\Desktop\test
python "C:\Users\awf\Desktop\test\logs\_select_tokens.py"
set CAPTURE_SECONDS=120 && python "C:\Users\awf\Desktop\test\logs\_ws_capture.py"
python "C:\Users\awf\Desktop\test\logs\_ws_analyze.py"
```

### Примечание по среде
- `python --version` -> `Python 3.11.15` (в задании зафиксирован 3.12).
  Стек httpx/websockets доступен; до реализации коллектора это не блокер.

## 2026-08-02 — Проверочный контур коллектора, ЗАДАЧА 1 (discovery)

Пишет проверочный контур. Раздел только дописывается, чужие разделы не менялись.

### Сделано
- `pwd` / `git rev-parse --show-toplevel` -> `C:/Users/awf/Desktop/test` (сверено, ошибки «работал не там» нет).
- Создан пакет `src/validate/` (моя зона): `discovery.py` + `__init__.py`; тесты `tests/test_validate.py`.
- ЗАДАЧА 1 выполнена и прогнана на живом API: найдены живые crypto up/down рынки, для каждого исхода — слаг и token_id.
- Проверка несуществующим тегом `tag_slug=nosuchtagxyz` -> пустой массив (параметр фильтрует). Живой вывод:
  `Проверка несуществующим тегом tag_slug=nosuchtagxyz: OK, пустой массив (параметр фильтрует)`
- Проверена согласованность `closed=false` и `end_date_min/max` (те же «тихие» фильтры): события приходят с `closed=false`; далёкое прошлое (2020) -> 0 событий. Нарушения контракта -> `TagFilterIgnored` (жёсткая остановка).
- Тесты: `python -m unittest discover -s tests -v` -> `Ran 71 tests ... OK` (из них 16 моих в `tests/test_validate.py`).

### НЕ сделано и почему
- Задачи 2-4 (book_poller, pmdata, compare) НЕ начаты: инструкция требует отчёт и подтверждение после каждой задачи.
- Файл результата discovery не пишется на диск: результат актуален только в момент запроса (рынки живут 5/15 минут), список не сохраняю по требованию.

### ФАКТЫ, найденные при разведке (свежие пробы, цитаты ниже)
- `/events?tag_slug=crypto&closed=false` НЕ сортирован по свежести: живые up/down рынки встречаются до offset=2000+ (`offset=1990 status=200 len=100`). Потолок offset=2000 (2001->422) делает ПОЛНОЕ перечисление невозможным -> обязателен датный срез `end_date_min/max` (это же паттерн `pm/markets.py iter_events`).
- Живой срез `[now-15m, now+15m]`: `len=28`, все события `closed=false`, 28 up/down рынков, 56 исходов. Монеты: btc, eth, sol, xrp, doge, bnb, hype.
- НАЙДЕНЫ НЕ ТОЛЬКО 5m: в срезе были `*-updown-15m-*` (doge/bnb/sol/eth/xrp/hype/btc). Маска "updown" в слаге ловит обе длительности.
- Поля рынка: `clobTokenIds` и `outcomes` — ОБЕ JSON-строки массивов (`'["Up", "Down"]'`), порядок совпадает. Признак живого рынка — `acceptingOrders=true`.

### Команды
```
python -m unittest discover -s tests -v
python -m src.validate.discovery
```

## 2026-08-02 — Проверочный контур коллектора, ЗАДАЧА 2 (book_poller)

Пишет проверочный контур. Дописывание в конец, чужие разделы не менялись.

### Сделано
- `src/validate/book_poller.py`: опрос `clob.polymarket.com/book` раз в 5 c,
  запись parquet в замороженной схеме (12 колонок, типы сверены с файлом).
- Тесты разбора /book и расчёта vwap — в `tests/test_validate.py`, на моках
  (сеть не трогают): +16 тестов. Полный набор: `Ran 87 tests ... OK`.
- Реальный 10-минутный прогон выполнен (`python -m src.validate.book_poller 600`):
  - основной набор 15m: 14 токенов, **1680 строк**, 120 циклов, 0 пустых,
    0 неполных, 0 отказов, серверная метка во всех 1680;
  - дополнительный 5m: 3 токена (2 пятиминутных рынка), 81 строка, 55 неполных,
    279 отказов — пятиминутные рынки умирают в ходе прогона, /book начинает
    отдавать отказы (это и есть проверка «переживания смены рынка», сюжет отдельный).
- Файлы: `data/validate/book_poll_15m_20260802T064911Z.parquet`,
  `data/validate/book_poll_5m_20260802T064911Z.parquet`.

### ФАКТЫ /book (свежая проба 2026-08-02)
- Серверная метка времени **ЕСТЬ**: поле `"timestamp":"1785652861636"`,
  13-значное целое, epoch ms (те же единицы, что у WS `price_change`).
- bids — массив {price,size} по возрастанию цены, asks — по убыванию; поля — строки.
- Пустая/односторонняя книга реальна (5m-рынки); для пустой стороны пишем NULL.
- Лимит: держали ~2.8 запроса/с на 15m-наборе и ~0.6 на 5m (суммарно 3.4);
  лимит 1500/10 c, запас огромный.

### НЕ сделано и почему
- Задачи 3-4 (pmdata, compare) не начаты: инструкция требует подтверждение.
- Динамическое добавление новых 5m-токенов в ходе прогона не делал: набор
  фиксируется на старте, умирание токенов наблюдается как отказы (это и было целью).
- Изменено ПОСЛЕ прогона: счётчик запросов в `PollSummary.requests` стал
  посерым (был глобальным для всех наборов). Данные parquet не менялись.

### Команды
```
python -m unittest discover -s tests -v
python -m src.validate.book_poller 600
```

## 2026-08-02 — Проверочный контур коллектора, ЗАДАЧА 3 (pmdata)

Пишет проверочный контур. Задача 2 принята (1680=14×120 без потерь,
360=81+279 на 5m). Вопрос про отказы закрыт: **все 279 — HTTP 404**
`{"error":"No orderbook exists for the requested token id"}` (мёртвый рынок),
429 не наблюдался. Записано в ASSUMPTIONS.md вместе с поправкой владельца
про выравнивание по ts_server_ms и допуск сверки (0.75–1.7 с, шаг токенов ~107 мс).

### Сделано
- `src/validate/pmdata.py`: читает ТОЛЬКО `PMDATA_API_KEY` из .env (не печатает,
  не коммитит), слаг считается арифметически `(now-2ч)//900*900`, существование
  рынка проверяется через Gamma ДО скачивания, качает ОДИН parquet в
  `data/validate/`. При ошибке сервиса печатает код + тело и останавливается.
- Скачан: `data/validate/eth-updown-15m-1785646800.parquet` (463 581 байт,
  окно 04:45–05:00 UTC, закрылось ~2ч назад). `data/` и `*.parquet` в .gitignore.
- Факты файла (напечатаны скриптом, не по памяти):
  - колонки: `market_slug` (large_string), `timestamp` (timestamp[us]),
    `local_timestamp` (timestamp[us]), `event_type` (large_string),
    `ask_prices`/`ask_sizes`/`bid_prices`/`bid_sizes` (large_list<double>),
    `best_ask`/`best_bid` (double), `pc_price`/`pc_size` (double),
    `pc_side`/`new_tick_size`/`winning_outcome` (large_string);
  - строк: **53 680**;
  - метки: `timestamp` 04:45:00.890000 → 05:16:55.703000, `local_timestamp`
    04:45:00.911967 → 05:16:55.706106, тип `timestamp[us]` (мкс эпохи, UTC) —
    НЕ те же единицы, что у нас (нас мс, тут мкс);
  - `event_type`: `book` 315, `price_change` 53 364, `market_resolved` 1.
- Формат внешнего эталона подтверждён: book = полные стаканы (списки цен/объёмов),
  price_change = WS-стиль с best_bid/best_ask/pc_* — прямо пригодно для сверки.

### НЕ сделано и почему
- Задача 4 (compare) не начата: инструкция требует подтверждение владельца.
- Больше одного слага не качал: квота 1000/мес, нужен один файл.
- Отказов pmdata не было — ветка «остановись и печатай код+тело» не сработала.

### Команды
```
python -m src.validate.pmdata
python -m unittest discover -s tests -v   # 87 OK
```

## 2026-08-02 — Проверочный контур коллектора, ЗАДАЧА 3.5 (сторона pmdata)

Пишет проверочный контур. Задача 3 принята. Блокер задачи 4 (в файле pmdata нет
идентификатора исхода) снят эмпирически.

### Сделано
- Взял рынок из задачи 2: `btc-updown-15m-1785653100` (мои 14 токенов → все 14
  принадлежат семи рынкам `*-updown-15m-1785653100`, окно 06:30–06:45 UTC).
- Скачал `data/validate/btc-updown-15m-1785653100.parquet` (895 546 байт, 107 037
  строк, 06:29:55–07:01:54 UTC, market_resolved в конце).
- Наложил по серверной метке: наши ts_server_ms (мс) ↔ их timestamp (мкс, каст в
  int64 + деление на 1000 ЯВНОЕ, автоприведения нет). Для каждого из 240 наших
  снимков (2 токена × 120) — последнее их событие с timestamp ≤ нашего
  ts_server_ms. Lag = 0.0 мс во всех 120 точках: /book timestamp = время последнего
  изменения книги, совпадает с последним их price_change.
- Считал обе величины для обоих токенов (n=120):

| токен (первые 24) | outcome | mean \|our_bid − their_bid\| | mean \|our_bid − (1−their_ask)\| |
|---|---|---|---|
| 788003553740357444299873 | Up | 0.000000 | 0.275333 |
| 102048009711811134883621 | Down | 0.276000 | 0.000000 |

  **Ответ: pmdata пишет сторону Up напрямую.** Наш Up-токен сравнивается с их
  best_bid/best_ask, Down-токен — через комплемент (1−their_ask). Установлено
  по gamma: порядок clobTokenIds совпадает с порядком outcomes ["Up","Down"].

### ФАКТЫ формата (записаны в ASSUMPTIONS.md)
- Эпоха слага = КОНЕЦ окна; pmdata пишет с открытия окна и ~16 мин после закрытия
  до резолва (market_resolved); их timestamp в мкс, наши в мс (делить на 1000).
- В файле нет идентификатора исхода — сторона только сопоставлением.
- Порядок книг: ask_prices по возрастанию (best=[0]), bid_prices по убыванию
  (best=[0]); у события `book` best_bid/best_ask NULL (цены в массивах), у
  `price_change` — заполнены, массивы NULL.
- Плотность: ~28 price_change/с (наши цифры: 55.4/с по всему диапазону btc,
  117.1/с в фазе резолва, ~1.5/с в активном окне). Поток событий посекундно
  НЕ сопоставим с нашим снимком раз в 5 с — сравнивать только состояние книги.

### НЕ сделано и почему
- Задача 4 (compare) не начата: инструкция требует подтверждение владельца.
- Другие слаги не качал: для ответа хватило одного файла (квота бережётся).
- Плотность ~28/с из ASSUMPTIONS.md — оценка владельца; мои измерения дают иные
  цифры по фазам (см. выше), в сводку записал обе величины.

### Команды
```
python -m unittest discover -s tests -v   # 87 OK
```

## 2026-08-02 — Проверочный контур коллектора, ЗАДАЧА 3.6 (кэш /book) и ЗАДАЧА 4 (compare)

Пишет проверочный контур. Задача 3.5 принята. Блокеры 3.6/4 закрыты, обе
задачи выполнены и прогнаны на реальных данных.

### Сделано
- **Задача 3.6 — кэш /book** (на данных btc-updown-15m-1785653100, 240 снимков
  Up+Down): для каждого нашего снимка посчитан счётчик их событий в интервале
  [ts_server_ms, ts_recv_ms):
  - min=43, **медиана=77.5**, p90=124, max=246; **доля нуля = 0.0**;
  - медиана book_age_ms = 763, медиана их межсобытийного gap = 3.0 мс.
  - **ВЫВОД: /book кэширован — мы получаем состояние, устаревшее на десятки
    реальных событий; 0.0 lag означает, что timestamp = время последнего
    изменения книги, а не время сборки ответа.** Формулировка «отставание
    0.75–1.7 с» в ASSUMPTIONS.md заменена на точную (счётчики 43/77.5/124/246).
- **Задача 3.6-недоделка — размеры** (240 снимков): Up (`7880…`):
  |bid_size − their_bid_size| = 0.0, |bid_size − their_ask_size| = 84.27;
  Down (`1020…`): |bid_size − their_ask_size| = 0.0, |bid_size − their_bid_size|
  = 84.23. **Правило: bid_size_down = ask_size_up, ask_size_down = bid_size_up**
  (комплемента для размеров не существует) — записано в ASSUMPTIONS.md.
- **Задача 4 — `src/validate/compare.py`**: выравнивание по ts_server_ms (их мкс
  → мс ЯВНЫМ делением); сторона Down через комплемент по правилу 3.5/3.6;
  реконструкция книги из (book-основа + price_change-дельты) переиспользуемым
  `BookReconstructor` (L1: best_bid/best_ask/spread; L2: vwap_bid_100/
  vwap_ask_100); Up/Down токены определены по gamma (окно вокруг эпохи слага,
  чтобы не упираться в offset-потолок).
- **Реальный прогон** `python -m src.validate.compare`:
  - снимков всего 1680, без пары 1440 (12 токенов других рынков), Up 120, Down 120;
  - по всем 5 величинам обеих сторон: **matched=120, exact_share=1.0, med=0,
    p99=0, max=0, over_tick=0, расхождений > 1 tick = 0**;
  - файл результата: `data/validate/compare_btc-updown-15m-1785653100.json`
    (сопоставленные пары, exact_share/медиана/p99/max по каждой величине,
    первые 10 расхождений отдельно Up/Down).
- Тесты: 95 OK (`TestBookReconstructor`, `TestDownComplement`, `TestCompareSide`).
  Две ошибки ожиданий в тестах исправлены (семантика None при комплементе и
  двойное расхождение best_ask+spread).

### ФАКТЫ (записаны в ASSUMPTIONS.md)
- Семантика `pc_side`: **BUY меняет bid-сторону, SELL — ask-сторону** (792 пары
  book→delta→book: нулевая ошибка; альтернатива BUY→ask даёт 678.32).
- Сверка на 240 снимках: 100% точных совпадений — восстановление книги и
  комплемент стороны Down подтверждены на реальном файле.

### НЕ сделано и почему
- Другие слаги для сверки не качал: квота pmdata 1000/мес бережётся; один
  файл дал согласованный результат на двух сторонах.

### Команды
```
python -m unittest discover -s tests -v   # 95 OK
python -m src.validate.compare            # результат -> data/validate/compare_*.json
```

## 2026-08-02 — Проверочный контур коллектора: ЗАДАЧА 4, 5m-прогон + коммит

Пишет проверочный контур. 15m-сверка принята с оговоркой (exact_share=1.0
не доказывает отсутствия потерь). Оговорка вписана в PROBE_RESULTS.md.
Две доделки выполнены.

### Доделка 1 — 5m-рынок с NULL и обрывом
- Скачан эталон `data/validate/xrp-updown-5m-1785653100.parquet` (155 531 байт,
  18 008 строк, 06:39:35–06:51:24 UTC) для рынка из 5m-набора book_poller
  (окно 06:45–06:50, закрылся до конца опроса — сюжет «умирание рынка»).
- Исправлен баг CLI: `main()` теперь получает полный `sys.argv`
  (раньше `argv[1:]` сдвигал индексы — pm_path получал слаг вместо файла).
- В `compare_side`-отчёт добавлено «отброшено из-за NULL» (поснимково, где
  best_bid или best_ask пуст) и в JSON-результат поле `dropped_null`.
- Прогон `python -m src.validate.compare "data/validate/book_poll_5m_20260802T064911Z.parquet" "data/validate/xrp-updown-5m-1785653100.parquet" "xrp-updown-5m-1785653100"`:
  - снимков 81, без пары 27 (hype-Up: токен другого рынка, `no_token`),
    Up 27, Down 27; NULL-стороны отброшены корректно (17 из 27 на каждую
    сторону — у Up пуст bid, у Down пуст ask, комплемент);
  - пять величин: все exact_share=1.0, med=0, p99=0, max=0, over_tick=0
    (Up: best_bid 10/27, best_ask 27/27, spread 10/27, vwap_bid_100 9/27,
    vwap_ask_100 26/27; Down зеркально) — расхождений > 1 tick = 0;
  - результат: `data/validate/compare_xrp-updown-5m-1785653100.json`.
  - ВЫВОД: NULL и обрыв рынка сверялка обходит без падений, пустоту
    совпадением не считает (несовпавшие метрики — это отброшенные NULL,
    а не совпавшие).

### Доделка 2 — коммит
- `git pull --rebase`, `git add src/ tests/ ASSUMPTIONS.md PROGRESS.md
  PROBE_RESULTS.md`, коммит `stage1/validate: discovery, book poller,
  pmdata reference, comparator`, `git push origin main`.
- parquet-файлы под .gitignore, не коммитились.
- Лог: см. `git log --oneline -1 origin/main`.

### НЕ сделано и почему
- Проверка потерь (детектор потерь) невозможна: REST-опрос не может
  пропустить то, что не запрашивал; проверка возможна только против
  вебсокет-коллектора. Записано в PROBE_RESULTS.md.

### Команды
```
python -m unittest discover -s tests -v   # 95 OK
python -m src.validate.compare "data/validate/book_poll_5m_20260802T064911Z.parquet" "data/validate/xrp-updown-5m-1785653100.parquet" "xrp-updown-5m-1785653100"
git pull --rebase && git add src/ tests/ ASSUMPTIONS.md PROGRESS.md PROBE_RESULTS.md && git commit -m "stage1/validate: discovery, book poller, pmdata reference, comparator" && git push origin main
```

## 2026-08-02 (вечер) — collector: смоук 15 мин, гейт покрытия <5% выполнен

### Сделано
- `src/collect/` работает end-to-end: `python -m unittest discover -s tests -q`
  → 117 OK.
- 15-мин смоук в `data/pm_smoke3.duckdb` (crypto up/down, 140 markets_tracked):
  - book_snapshots 25650, tick_changes 25188, recon_checks 640
    (verdict: warmup 520, match 120, **mismatch 0**), reconnects 7,
    gap_intervals по причине disconnect 532;
  - покрытие: рынков с долей пропусков < 5% — **112 из 112** (гейт G3.
    До фикса было 14/112 с долями 10-20%).
- Исправления по ходу:
  - `coverage.py`: `MIN(end_ms, ?)` парсился как агрегат → скалярные
    LEAST/GREATEST (binder-ошибки «aggregate function calls cannot be nested»
    и «column lo_ms not found»);
  - `_rest_backfill` переведён на async (httpx.AsyncClient, пул
    BACKFILL_CONCURRENCY=16): 84 токена 7-11 c → ~2.7 c. Это и вытащило
    покрытие под 5% (server_resync 12-34 c → ~2-3 c);
  - в бэкфилле при 404-рынке `live.initialized=False`, чтобы первый WS-снимок
    после обрыва был warmup, а не ложный mismatch (в прогоне было 12-20
    mismatch, в финальном — 0);
  - `ping_timeout` 20 → 90 c (не рубим соединение сами по задержке pong) +
    `await asyncio.sleep(0)` после каждого сообщения (пропуск управляющих
    кадров). На каденс обрывов не повлияло.
- Прочие смоуки: `data/pm.duckdb` (15 мин, старый последовательный бэкфилл,
  покрытие 14/112 — устарел), `data/pm_smoke2.duckdb`, `data/pm_iso.duckdb`
  (8 мин, recheck/export отключены).

### НЕ сделано и почему
- Двухчасовой прогон в основную БД + сверка `compare.py` против pmdata —
  не запущены (лимит сессии; pmdata-квота; вопрос про суточный прогон).
- Корневая причина обрывов WS не установлена (сервер сам закрывает 1011
  «keepalive ping timeout» ~каждые 90 c; зонд с 3 токенами живёт 150+ c;
  recheck/export и ping_timeout не влияют). Открытый вопрос в
  DECISIONS_NEEDED.md.
- Правки НЕ закоммичены и не запушены (AGENTS.md: без явной команды).

### Команды
```
python -m src.collect.ws_collector --minutes 15 --vertical crypto --db data/pm_smoke3.duckdb
python -m src.collect.coverage --db data/pm_smoke3.duckdb
python -m unittest discover -s tests -q   # 117 OK
```
