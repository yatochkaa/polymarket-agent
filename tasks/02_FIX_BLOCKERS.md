# Задание 02 — Три правки-блокера, строго по порядку

Сессия без памяти. Кредитов мало. Порядок правок — не рекомендация:
правка 2 бессмысленна без правки 1, правка 3 самая дешёвая и идёт
последней.

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

1. `PREREGISTRATION.md` — источник порогов и определений
2. `AUDIT_REPORT.md`
3. `probe.py` — строки 1–60 и 170–200
4. `pm/experiments/__init__.py`
5. `pm/fees.py` целиком
6. `pm/experiments/e1_prices_history.py` — как образец стиля модуля
7. `pm/experiments/e4_tennis.py` — строки 90–170
8. `tests/test_fees.py`

Не открывай `.env`. Не читай `.claude/`.

## 1. Уже известно — НЕ перепроверяй

- `import probe` падает на `probe.py:41`:
  `ImportError: cannot import name 'e2_fee_basis' from 'pm.experiments'`.
  Файл `pm/experiments/e2_fee_basis.py` отсутствует.
- Контракт восстановлен ПО МЕСТАМ ВЫЗОВА, это реконструкция, не факт:
  - `probe.py:41` — `from pm.experiments import e2_fee_basis as e2`
  - `probe.py:186` — `e2.run(s, data, vertical=Vertical(args.vertical))`
  - `probe.py:187` — `e2.report_dict(r2)`
  - `probe.py:193` — `e2.decode_orderfilled(s.rpc_url, args.tx)`
  - `pm/experiments/__init__.py:9` — нужен экспорт
  Неизвестно: точные поля `r2`, что возвращает `decode_orderfilled`,
  какие исключения ожидаются.
- Тесты сейчас 35/35 зелёные ⇒ Э2 не покрыт тестами вообще.
- Гейт мощности: `MIN_EVENTS_PER_TRADER = 30` сравнивается с общим
  числом событий на рынке, а не на трейдера. Диагноз аудита
  правдоподобен, но ТОЧНАЯ строка сравнения аудитом не приведена —
  её надо найти и процитировать перед правкой.
- Решение владельца: единый порог 100, число 30 удаляется.
- Нумерация гейтов сломана в трёх местах, везде G1 вместо G4:
  `e4_tennis.py:102`, `TASK.md:112-114`, `TASK_AUDIT.md:74-75`.
- Все числа комиссий живут только в `pm/fees.py`.
- Ставки: Crypto 0.07 / Sports 0.05 / Politics 0.04 / Geopolitics 0.
- `pm/broker.py` отсутствует НАМЕРЕННО. Не создавать.

## 2. Шаги

### Правка 1 — создать `pm/experiments/e2_fee_basis.py`

**Шаг 1.1.** Сними точный контракт с мест вызова:

```bash
sed -n '30,50p;175,200p' probe.py
grep -rn "e2_fee_basis\|e2\." probe.py pm/ tests/
sed -n '1,20p' pm/experiments/__init__.py
grep -n "class \|def \|Literal\|raise " pm/fees.py
```

**Шаг 1.2.** Выпиши в черновик фактические сигнатуры: имена аргументов,
тип `s` (settings), тип `data`, тип `Vertical`, как используется
результат `report_dict`. Если фактический вызов расходится
с реконструкцией выше хотя бы в одном имени или порядке аргументов —
**ОСТАНОВИСЬ**. Не правь `probe.py` под свой модуль. Запиши расхождение
в `DECISIONS_NEEDED.md` в формате:

```text
## Э2, расхождение контракта (дата)
Ожидалось: <строка из реконструкции>
Фактически: <файл:строка и точный код>
Вопрос владельцу: <что выбрать>
Сессия остановлена, правки 2 и 3 не выполнялись.
```

и заверши сессию.

**Шаг 1.3.** Если расхождений нет — реализуй модуль:

- `run(s, data, *, vertical: Vertical) -> E2Result` — сравнивает
  наблюдаемую комиссию с двумя гипотезами базы (`shares`, `notional`)
  и возвращает результат с полями как минимум:
  `basis` (`"shares" | "notional" | "unknown"`), `n_observations`,
  `residual_shares`, `residual_notional`, `fee_rate`, `vertical`,
  `resolved` (bool), `evidence` (список разобранных сделок).
- `report_dict(r2) -> dict` — плоский JSON-сериализуемый словарь
  для `PROBE_RESULTS.md`, без объектов и без numpy-типов.
- `decode_orderfilled(rpc_url: str, tx: str) -> dict` — декодирует
  событие `OrderFilled` и возвращает как минимум
  `maker_amount`, `taker_amount`, `fee`, `price`, `is_taker`.
- Ни одного числового значения комиссии в этом файле:
  все ставки берутся из `pm/fees.py`.
- При невозможности различить гипотезы — `basis = "unknown"`,
  `resolved = False`. НЕ угадывать.
- Запись результата — только через `pm.fees.record_e2_result(...)`
  и только из `probe.py`, не изнутри `run`.

**Шаг 1.4.** Экспортируй модуль в `pm/experiments/__init__.py`
(строка 9, рядом с остальными).

**Шаг 1.5.** Проверь, что `taker_fee` без разрешённой базы падает.

СНАЧАЛА сними фактическую сигнатуру, не угадывай имена аргументов:

```bash
python -c "import pm.fees as f, inspect; print(inspect.signature(f.taker_fee))"
```

Потом вызови её ИМЕННО с теми аргументами, которые вывела
команда выше, и различи два разных падения:

```bash
python -c "import pm.fees as f; \
import traceback; \
try: r = f.taker_fee(<аргументы по фактической сигнатуре>); print('RETURNED', r); \
except f.FeeBasisUnresolved: print('OK_RAISES_UNRESOLVED'); \
except TypeError as e: print('WRONG_CALL', e)"
```

Ожидается `OK_RAISES_UNRESOLVED`.

- `WRONG_CALL` НЕ считается успехом. Это твоя ошибка вызова,
  а не защита кода. Исправь вызов и повтори.
- `RETURNED <число>` — дефект: без результата Э2 комиссия считаться
  не должна. Почини и добавь тест. Угадывание базы недопустимо.

**Шаг 1.6.** Проверка импорта:

```bash
python -c "import probe; print('IMPORT_OK')"
```

### Правка 2 — гейт мощности, ДВЕ части

**Часть A — найти и процитировать точную строку.**

```bash
grep -rn "MIN_EVENTS_PER_TRADER" . --include=*.py
grep -rn "len(events)\|n_events\|events_count\|>= MIN_" pm/ --include=*.py
```

Выпиши в `PROGRESS.md` найденную строку целиком в виде
`файл:строка: <код>` ДО того, как что-либо править. Если сравнение
уже идёт на уровне трейдера — не правь, а запиши расхождение
с аудитом в `DECISIONS_NEEDED.md` и переходи к части B только после
явного вывода о том, что реально происходит.

**Часть B — исправить уровень агрегации, затем порог.**

1. Сначала измени сравнение так, чтобы оно шло по числу событий
   (матчей) КОНКРЕТНОГО трейдера, а не по общему числу событий рынка.
2. Напиши тест, который **падает на старом поведении**: набор данных,
   где на рынке 500 событий, но у трейдера 12 матчей. Старый код
   гейт закрывает, новый — обязан не закрыть. Файл
   `tests/test_power_gate.py`.
3. Убедись, что новый тест падает до правки. Порядок: тест → красный →
   правка → зелёный. Если тест зелёный до правки — он неправильный,
   переписывай.
4. Только после этого поставь `MIN_EVENTS_PER_TRADER = 100`
   в `pm/config.py` и удали число 30 из кода и комментариев.

**Замена только числа 30 на 100 без части A и без теста —
НЕДОПУСТИМА.** Это скроет поломку и сделает гейт декоративным.

```bash
grep -rn "\b30\b" pm/ --include=*.py | grep -i "event\|trader\|min"
```

Должно быть пусто.

### Правка 3 — нумерация гейтов G1 → G4

Ровно три места, ничего больше:

```bash
sed -n '100,104p' pm/experiments/e4_tennis.py
sed -n '110,116p' TASK.md
sed -n '72,77p' TASK_AUDIT.md
```

Правь точечно, вручную, по одной строке. После правки:

```bash
grep -rn "G1" pm/experiments/e4_tennis.py TASK.md TASK_AUDIT.md
```

Оставшиеся вхождения `G1` должны относиться только к настоящему
гейту G1 (цена mid/last trade). Всё, что про мощность — G4.

## 3. Запреты

- Не менять `probe.py` под свою реализацию Э2.
- Не создавать `pm/broker.py`, не отправлять ордера.
- Не запускать `probe.py` — это задание 03.
- Не менять пороги, кроме 30 → 100 в рамках правки 2 части B.
- Не трогать `PREREGISTRATION.md` (он заморожен).
- Не ставить пакеты вне `requirements.txt`.
- Не использовать `py-clob-client` (архивирован и запрещён).
- Не делать `git add`, `git commit`, `git push`.
- Не читать `.env`.
- Не рефакторить ничего сверх трёх правок.

## 4. Критерий завершения (проверяемые команды)

```bash
python -c "import probe; print('IMPORT_OK')"
python -m unittest discover -s tests -v
grep -rn "MIN_EVENTS_PER_TRADER" pm/ --include=*.py
grep -rn "0\.07\|0\.05\|0\.04" --include=*.py . | grep -v fees.py | grep -v tests/
git status --short
git diff --stat
```

Ожидается: `IMPORT_OK`; unittest зелёный и тестов строго больше 35
(добавлены тесты Э2 и `tests/test_power_gate.py`); порог равен 100;
grep по ставкам комиссий пуст; в `git diff --stat` только
ожидаемые файлы.

## 5. Завершение сессии

- В `PROGRESS.md`: дата, что сделано по каждой из трёх правок,
  процитированная строка гейта мощности до и после, число тестов
  до и после, вывод команд из раздела 4.
- В `DECISIONS_NEEDED.md`: все места, где контракт Э2 пришлось
  домыслить; всё, что осталось непокрытым тестами.
- Коммит НЕ делать.
