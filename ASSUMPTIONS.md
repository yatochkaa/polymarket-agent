# ASSUMPTIONS.md

## Фактические assumptions из логов — 2026-08-03

- **[подтверждено логом]** `/trades` с `offset>10000` может возвращать HTTP 400 для отмеченного рынка; полнота хвоста pre-match автоматически не следует. Источник: `logs/collect_dryrun.log`, `logs/collect_window.log`.
- **[подтверждено логом]** В проверенном выводе `/positions` `limit500=8`, `limit1000=8`, `limit1000_offset_n=0`, `offset5000=0`. Это не подтверждает поведение для всех аккаунтов. Источник: `probes/glm/probe2_run.log`, `probes/glm/probe2_summary.json`.
- **[подтверждено логом]** В просмотренном websocket-наборе timestamp встречался `174` раз, sequence-number field не обнаружен. Это не доказывает отсутствия sequence number в других типах сообщений. Источник: `logs/_ws_analysis.txt`.
- **[вероятно]** Единицей timestamp websocket является epoch milliseconds: лог прямо помечает это как `guessed units: epoch_milliseconds`, а не как независимую верификацию. Источник: `logs/_ws_analysis.txt`.
- **[подтверждено логом]** `unknown_types=0` не означает корректную реконструкцию: в smoke run `recons_mismatch=12`, в tennis run `recons_mismatch=100`. Источник: `logs/collector_smoke.log`, `logs/probe_tennis.log`.
- **[подтверждено логом]** В neg5 run измерены `dropped=559` и `recons_mismatch=47`; этот лог нельзя считать доказательством отсутствия потерь. Источник: `logs/neg5_run.log`.
- **[подтверждено логом]** Для pullstart-прогона `5393/5393 slugs`, `gameStartTime present=5393`, `null=0`, `failed_slugs=0`; это свойство конкретного запуска, а не универсальная гарантия API. Источник: `logs/filter7_pullstart.log`.
- **[подтверждено логом]** В feasibility-выводе ITF имеет `0` строк и `nan` по производным полям; ITF-метрики этим прогоном не измерены. Источник: `logs/filter7_feasibility.log`.
- **[вероятно]** `VERDICT: ALL GREEN on judged tiers` означает зелёный статус только для judged tiers: тот же лог сообщает `NO survivor stats computed (no gross_delta / no t / no pass counts)`. Источник: `logs/filter7_degeneracy.log`.
- **[предположение]** Ненулевой размер файла недостаточен для признания прогона завершённым: фактические traceback и обрывы есть в `probes/glm/probe1.log`, `probes/glm/probe1_window2.log`, `probes/glm/probe3_run.log`, `probes/opus/probe3b_run.log`. Источник: указанные логи.

## Факт о hash в price_change — 2026-08-03

- **[подтверждено логом]** Поле `hash` в элементе `price_changes` НЕ уникально: сервер переиспользует одно и то же значение hash для РАЗНЫХ изменений одного `asset_id` в пределах одной миллисекунды (два отдельных сообщения с одинаковым `recv_ms`). Источник: `logs/ws_raw.jsonl`, пара `recv_ms=1785651207744` — `price=0.3 size=8700` против `price=0.4 size=6781.44` с одним hash `5d37d11d...`.
- **[подтверждено логом]** По всему захвату `logs/ws_raw.jsonl`: 8 коллизий ключа `(asset_id, hash)` на 358 price_change-событий; из них 6 — РАЗНЫЕ изменения (разные price/size) с общим hash, 2 — дословные копии. То есть ключ дедупа обязан включать price и size: `(asset_id, hash, price, size)` — иначе легитимная дельта отбрасывается.
- **[подтверждено логом]** Данные суток от 2026-08-03 собраны СТАРЫМ ключом `(asset_id, hash)`: около 1.7% дельт не применены к книге; независимый контроль recon при этом `6/932` (0.6%) — расхождение с эталоном в пределах шума, массовой потери книги нет. Источник: прогон 2026-08-03, `logs/` (recons_mismatch 6 из 932).
