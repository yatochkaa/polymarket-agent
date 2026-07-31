# Задание 03 — Прогнать probe.py и закрыть G1 и G2

Сессия без памяти. Кредитов мало. Это первый запуск реального кода
в проекте: до сих пор ни один эксперимент не исполнялся.

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
2. `PROGRESS.md` — последние записи заданий 01 и 02
3. `probe.py` целиком
4. `pm/fees.py` — `record_e2_result`, `FeeQuote`, `FeeBasisUnresolved`
5. `pm/experiments/e1_prices_history.py`
6. `pm/experiments/e2_fee_basis.py`
7. `pm/experiments/e4_tennis.py`
8. `pm/config.py`

Не открывай `.env`, но помни: ключи и RPC берутся оттуда окружением.

## 1. Уже известно — НЕ перепроверяй

- CLOB V2 работает с 28.04.2026.
- `/orderbook-history` молча отдаёт `{"count": 0, "data": []}`
  начиная с 20.02.2026. Пустой ответ оттуда — не баг твоего кода.
- Залог — pUSD, не USDC.e.
- SDK — `polymarket-client` (`Polymarket/py-sdk`).
  `py-clob-client` архивирован и ЗАПРЕЩЁН.
- Отображаемая цена = mid, кроме spread > $0.10 → last trade.
  Э1 это проверяет, а не выясняет с нуля.
- Ставки: Crypto 0.07 / Sports 0.05 / Politics 0.04 / Geopolitics 0.
  Мейкеры не платят никогда.
- Лимиты: Gamma `/markets` 300/10s; `data-api/trades` 200/10s;
  `data-api/positions` 150/10s; `clob/book` 1500/10s;
  `clob/prices-history?fidelity=1` 1000/10s.
  Cloudflare замедляет, а не отклоняет — 429 лечится паузой,
  а не сменой эндпоинта.
- `record_e2_result` бросает `FileExistsError` при повторной записи.
  Это ЗАДУМАНО.
- Э3 требует реального ордера. Не запускать.

## 2. Шаги

### Шаг 0. Зависимости

В среде может не быть `pydantic_settings` и других пакетов. Установи
ровно то, что уже записано в `requirements.txt`, ничего не добавляя:

```bash
python -m pip install -r requirements.txt
```

Строка SDK в `requirements.txt` намеренно без версии. Если установка
на ней падает — ОСТАНОВИСЬ и запиши в `DECISIONS_NEEDED.md`.
НЕ выбирай версию сам, НЕ ставь `py-clob-client`.

### Шаг 1. Санитарная проверка перед запуском

```bash
mkdir -p logs
git log --oneline -10
git status --short
python -c "import probe; print('IMPORT_OK')"
python -m unittest discover -s tests -q
python probe.py --help
```

ФЛАГИ БЕРИ ТОЛЬКО ИЗ ВЫВОДА `--help`. Ниже они указаны как ожидаемые
(`--e1`, `--e2`, `--e4`, `--vertical`, `--tx`, `--all`), но если `--help`
показывает другое — используй то, что там написано, и запиши
расхождение в `PROGRESS.md`. Никакого `--exp`.

```bash
```

Если `import probe` падает — задание 02 не выполнено. Останови работу,
запиши это в `PROGRESS.md` и заверши сессию. Не чинить код здесь.

### Шаг 2. Э1 — цена mid или last trade (гейт G1)

```bash
python probe.py --e1 --vertical sports 2>&1 | tee logs/e1.log
```

Зафиксируй: число рынков в выборке, долю совпадений с mid,
долю совпадений с last trade, поведение при `spread > 0.10`,
дату и время прогона, точную команду.

Вердикт G1: `mid` / `last_trade` / `hybrid_by_spread` / `inconclusive`.
Если выборка меньше 30 рынков или доли различаются менее чем
на 10 п.п. — пиши `inconclusive`. Не выбирать «более удобный» ответ.

### Шаг 3. Э2 — база комиссии shares или notional (гейт G2)

```bash
python probe.py --e2 --vertical sports 2>&1 | tee logs/e2.log
```

При необходимости декодирования конкретной сделки:

```bash
python probe.py --e2 --tx <TX_HASH> 2>&1 | tee -a logs/e2.log
```

Результат Э2 пишется через `record_e2_result` ОДИН раз. Если запуск
повторяется и падает `FileExistsError` — это НОРМА, а не ошибка:
запиши факт в лог и не обходи защиту. Запрещено удалять файл
результата, менять имя, добавлять `force`, ловить и глотать
исключение.

Вердикт G2: `shares` / `notional` / `inconclusive`.
Если остаточные ошибки обеих гипотез сопоставимы — `inconclusive`,
и `FeeBasis` остаётся `"unknown"`.

Проверь, что при неразрешённой базе комиссия не считается:

```bash
python -c "import pm.fees as f; print(f.resolved_basis())"
```

### Шаг 4. Э4 — теннис, разведочный прогон

```bash
python probe.py --e4 --vertical sports 2>&1 | tee logs/e4.log
```

Зафиксируй: число найденных матчей, окно дат, число уникальных
адресов, сколько адресов имеет ≥100 матчей, сработал ли гейт
мощности G4 и на каком уровне агрегации.

Если гейт G4 закрывается мгновенно и на всех — это признак,
что правка задания 02 не подействовала. Запиши в
`DECISIONS_NEEDED.md`, не «чини на месте».

### Шаг 5. Э3 — НЕ запускать

В `PROBE_RESULTS.md` для Э3 строка: «Не запускался: требует реального
ордера. Запрещено заданием.» Никакого `--run-e3`.

### Шаг 6. Заполни `PROBE_RESULTS.md`

Одна секция на эксперимент, одинаковая структура:

```text
## Э<N> — <название>
- Дата и время (MSK):
- Команда:
- Коммит (git rev-parse --short HEAD):
- Размер выборки:
- Числа (таблица):
- Вердикт: <значение | inconclusive>
- Какой гейт закрывает: G<N> / нет
- Ограничения и что осталось неизвестным:
```

В конце файла — сводка гейтов:

```text
| Гейт | Статус | Основание |
|---|---|---|
| G1 | closed / open | Э1, <дата> |
| G2 | closed / open | Э2, <дата> |
| G3 | open | требует своего сборщика (задание 04) |
| G4 | open | требует данных |
```

## 3. Запреты

- Не запускать Э3 и не ставить ордера ни в каком виде.
- Не создавать `pm/broker.py`.
- Не обходить `FileExistsError` от `record_e2_result`.
- Не менять пороги, критерии и исходы после того, как увидел числа.
- Не менять `PREREGISTRATION.md`.
- Не «подкручивать» выборку, чтобы вердикт стал определённым.
- Не использовать `/orderbook-history` как источник истории стакана.
- Не использовать `py-clob-client`.
- Не ставить пакеты вне `requirements.txt`.
- Не делать `git add`, `git commit`, `git push`.
- Не печатать содержимое `.env` и ключей в логи.

## 4. Критерий завершения (проверяемые команды)

```bash
test -f PROBE_RESULTS.md && echo OK_FILE
grep -c "^## Э" PROBE_RESULTS.md
grep -n "Вердикт" PROBE_RESULTS.md
grep -n "| G1 |\|| G2 |\|| G3 |\|| G4 |" PROBE_RESULTS.md
ls -la logs/
python -m unittest discover -s tests -q
git status --short
```

Ожидается: `OK_FILE`; ровно 4 секции `## Э` (Э3 — с пометкой
«не запускался»); по одному «Вердикт» на секцию; таблица гейтов
из четырёх строк; логи `e1.log`, `e2.log`, `e4.log` непустые;
тесты зелёные; в `git status --short` нет удалённых файлов.

## 5. Завершение сессии

- В `PROGRESS.md`: дата, какие эксперименты прогнаны, какие гейты
  закрыты, какие остались открытыми и почему; полные команды.
- В `DECISIONS_NEEDED.md`: каждый `inconclusive` — отдельным пунктом
  с описанием, каких данных не хватило и что предлагается сделать.
- Коммит НЕ делать.
