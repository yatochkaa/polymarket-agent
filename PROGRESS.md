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
