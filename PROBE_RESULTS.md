# PROBE_RESULTS.md

## Пофайловое дополнение — 2026-08-03

Все числа ниже взяты непосредственно из указанного лога. Целый файл может содержать невалидный результат; это отмечено отдельно.

### `probes/glm/probe1_window1_done.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** перепись окна 1 по месяцам, наличие pre-match сделки и выборочные строки `/trades`.
- **Числа:** `4166 / 4285 = 97.2%`; `доля с предматчевой сделкой (60 min): 97.2%`; `2026-02: median=554.0 markets=1185 with_trades=1185 min=2 max=10500`; `2026-03: median=723.0 markets=1503 with_trades=1502 min=0 max=8378`; `2026-04: median=1073.0 markets=1597 with_trades=1597 min=2 max=8339`.
- **Вывод:** в окне 1 лог показывает 97.2% рынков с pre-match сделкой; полнота всех `/trades` этим не доказана.

### `probes/glm/probe1_window2.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** перепись окна 2 по месяцам и tier.
- **Числа:** `2026-05 ATP: median=978.0 markets=1033 with_trades=1033 min=2 max=10500`; `2026-05 WTA: median=948.5 markets=568 with_trades=568 min=2 max=9174`; `2026-05 ITF: median=18.0 markets=67 with_trades=66 min=0 max=177`; `2026-05 ATP+WTA: median=969.0 markets=1601 with_trades=1601 min=2 max=10500`; `2026-06 ATP: median=630.0 markets=1134 with_trades=1130 min=0 max=10500`; `2026-06 WTA: median=687.5 markets=696 with_trades=685 min=0 max=7199`; `2026-06 ITF: median=40.5 markets=104 with_trades=104 min=2 max=523`; `2026-06 ATP+WTA: median=645.0 markets=1830 with_trades=1815 min=0 max=10500`; `2026-07 ATP: median=654.0 markets=1155 with_trades=1125 min=0 max=9690`; `2026-07 WTA: median=631.0 markets=654 with_trades=614 min=0 max=6536`; `2026-07 ITF: median=74.0 markets=129 with_trades=129 min=4 max=455`; `2026-07 ATP+WTA: median=651.0 markets=1809 with_trades=1739 min=0 max=9690`; общий вывод: `ATP: median=719.0 markets=3322 with_trades=3288 min=0 max=10500`; `WTA: median=746.5 markets=1918 with_trades=1867 min=0 max=9174`; `ATP+WTA: median=726.5 markets=5240 with_trades=5155 min=0 max=10500`; `ITF: median=43.5 markets=300 with_trades=299 min=0 max=523`; `ALL: median=683.5 markets=5540 with_trades=5454 min=0 max=10500`.
- **Вывод:** распределения записаны; в файле также присутствует ошибка кодировки, поэтому статус — частично оборванный, а не пустой.

### `probes/glm/probe1.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** запуск GLM probe 1.
- **Числа:** измеренных чисел результата нет; в логе есть `can't open file ... <имя_файла>` и `Invalid argument`.
- **Вывод:** файл целый и непустой, но запуск не состоялся; результат не измерен.

### `probes/glm/probe3_run.log`
- **Дата:** не указано в логе; **скрипт:** `probe3_event_fields.py` (имя скрипта есть в каталоге, не утверждается как имя запуска). **Проверялось:** поля событий, матчевые рынки и ключ турнира.
- **Числа:** `Турниров с >=2 матчами: 371`; `POLYA RYNKA (match market) (81 poley)`; `POLYA RYNKA (match market) (78 poley)`; `ATP.series` имеет `id: 10365`; в последнем объекте `volume24hr: 3314217.387812999`, `volume: 2950503.864492001`, `liquidity: 12445123.73344`, `commentCount: 3158`.
- **Вывод:** лог показывает поля событий и tournament-candidate ticker, но уникальность ключа турнира не доказана.

### `probes/opus/probe3_run.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** фильтр 4 и доля совпадений.
- **Числа:** результат не извлечён из однострочного вывода с повреждённой кодировкой; `0.198` в этом логе не подтверждено.
- **Вывод:** файл целый и непустой, но результат фильтра 4 невалидируем по доступному представлению; это не пустой лог.

### `probes/opus/probe3b_run.log`
- **Дата:** не указано в логе; **скрипт:** `probe3b_filter4_recon.py`. **Проверялось:** повторы, фильтр 4 и recon.
- **Числа:** итоговых чисел нет; лог содержит `SSL: UNEXPECTED_EOF_WHILE_READING` и `RuntimeError`.
- **Вывод:** результат невалиден из-за сетевой ошибки; файл целый и непустой.

### `logs/filter5_clvage.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** CLV по возрасту эталона, price convergence, корреляция CLV с realized PnL и возраст первичного токена.
- **Числа:** ATP `usable records=343512`; по bins grossDelta: `0.2444`, `0.2091`, `0.0300`, `-0.2068`, `-0.2505`; ATP `n=343512 median=18.9 p25=4.3 p75=38.5 p90=125.5 share>15=0.539 share>60=0.200`; WTA `n=191025 median=19.3 p25=4.3 p75=37.9 p90=125.3 share>15=0.540 share>60=0.205`; ITF `n=108434 median=34.4 p25=23.0 p75=61.4 p90=130.3 share>15=0.833 share>60=0.254`; ITF grossDelta: `0.2038`, `0.2098`, `0.1971`, `-0.0243`, `-0.0137`.
- **Вывод:** показатели зависят от возраста эталона; ITF обозначен exploratory и не подтверждает decision-family результат.

### `logs/filter5_count.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** funnel, survivors и retention при нескольких Delta.
- **Числа:** `strict 15 min: matches=343841 incl=158418 stale=185094 no_etalon=0 no_resolve=329`; `soft 60 min: matches=343841 incl=274870 stale=68642 no_etalon=0 no_resolve=329`; `strict=401 soft=212 both_agree=156`; `control retention @strict=15 min: 46.1%`; WTA `matches=191198`, `strict incl=87822`, `soft incl=151786`, `strict=206 soft=112 both_agree=83`; `strict 30 min: matches=108476 incl=36523 stale=71911`; `soft 120 min: matches=108476 incl=93107 stale=15327`; `strict=52 soft=46 both_agree=32`.
- **Вывод:** сам лог помечает retention `46.1%` как `STOP: retention<50% at Delta_strict`.

### `logs/filter5_selfref.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** self-reference, near-self и base/nonself.
- **Числа:** `Delta=120 incl(base)=93107 self-ref=4929 (5.3%) near-self<60s=6932 (7.4%) incl(nonself)=92355`; `BASE strict=421 soft=116 both=94`; `NONSELF strict=414 soft=113 both=90`; `base=True nonself=True`.
- **Вывод:** counts меняются после исключения self-reference; единственная причина этим логом не доказана.

### `logs/filter7_window_probe.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** окно, структура событий/рынков, tournament key и raw `/trades`.
- **Числа:** `fetched closed=True events=19202 slices=26 capped_slices=0`; `fetched closed=False events=5 slices=26 capped_slices=0`; `ENUM: events=19207 singles_tier_markets=17478`; `OLD ... atp=2879 wta=1448 itf=0 total=4327`; `CUR ... atp=3085 wta=1785 itf=7892 total=12762`; `gameStartTime present=17478 absent=0 total_markets=17478`; sample `trades_returned=6`, `11`, `33`, `1333`, `1560`, `1020`, `395`, `1733`, все `truncated=False`.
- **Вывод:** в данном окне все 17478 enumerated markets имели gameStartTime; отсутствие ошибок вне проверок не доказано.

### `logs/filter7_feasibility.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** start signal, pre-match trades, wallet feasibility и возраст pre-match price.
- **Числа:** ATP `conds=3437 with gameStartTime=3437 (100.0%) traded markets checked=3295 first-trade-before-start=97.8% median(open-to-start)=1298.2 min`; WTA `conds=1956 with gameStartTime=1956 (100.0%) first-trade-before-start=97.4% median(open-to-start)=1310.6 min`; ITF `conds=8082 with gameStartTime=0 (0.0%) traded markets checked=0 first-trade-before-start=nan%`; candidate ATP `1652638` trades, `229633` pre_match, share `0.139`; WTA `941988`, `130945`, `0.139`; price age ATP `3295 3223 0.978 0.9 0.2 3.9 15.0`; WTA `1877 1829 0.974 0.7 0.2 3.3 19.3`.
- **Вывод:** ATP/WTA feasibility измерена; ITF не измерен из-за отсутствия start times.

### `logs/filter7_degeneracy.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** degeneracy, CLV vs realized profit, outcome prediction и self-reference.
- **Числа:** ATP `n_line=3223 share|x|>.95/.05=0.018 share 0.10-0.90=0.953 median|x-.5|=0.150 p90=0.350`; WTA `n_line=1829 ... 0.026 ... 0.924 ... 0.180 ... 0.381`; ATP correlation `n_trades=216674 corr=0.162 slope=0.730`; WTA `125318 corr=0.296 slope=1.041`; ATP prediction `n_pred=2924 favorite-won=0.674 Brier=0.204`; WTA `1681 0.700 0.193`; ATP self-reference `n_pairs=115295 ... 0.013 ... 0.044`; WTA `64615 ... 0.013 ... 0.056`; `VERDICT: ALL GREEN on judged tiers`; `NO survivor stats computed`.
- **Вывод:** judged ATP/WTA tiers прошли death-rule; survivor stats не измерялись, ITF exploratory.

### `logs/filter7_pullstart.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** pullstart и start_map.
- **Числа:** `pulled 5393/5393 slugs`; `mapped_conds=5393`; `gameStartTime present=5393 null=0 failed_slugs=0`; ATP `start present=3437 null=0`; WTA `start present=1956 null=0`.
- **Вывод:** в данном запуске все pulled slugs получили start time.

### `logs/neg5_run.log`
- **Дата:** 2026-08-02 присутствует в логе; **скрипт:** collector. **Проверялось:** websocket collector и backfill.
- **Числа:** `messages: 6002`; `unknown_types: 0`; `events: 12077`; `events_skipped_dedup: 6`; `snapshots: 11473`; `ticks: 11512`; `recons: 282`; `recons_mismatch: 47`; `reconnects: 2`; `dropped: 559`; `max_silence_s: 4.065`.
- **Вывод:** результат невалиден как доказательство lossless-сбора: в нём 559 dropped и 47 mismatches.

### `logs/probe_tennis.log`
- **Дата:** 2026-08-02 присутствует в логе; **скрипт:** collector. **Проверялось:** длительный tennis collector.
- **Числа:** промежуточно `recons: 4068`, `recons_mismatch: 98`; финал `messages: 156733`, `unknown_types: 0`, `events: 307444`, `events_skipped_dedup: 394`, `snapshots: 305018`, `ticks: 307050`, `recons: 4266`, `recons_mismatch: 100`, `reconnects: 1`, `dropped: 0`.
- **Вывод:** dropped=0 в финале не доказывает корректность реконструкции, поскольку mismatch=100.

### `logs/collect_dryrun.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** dry-run окна и preflight.
- **Числа:** `ATP+WTA в окне (gst) = 188`; `эталон полного перебора ~4068`; допустимо `4050..4110`; `dryrun: контроль 4068 НЕ применяется (окно другое)`; далее `1 рынков имеют >=20000 сделок`; `offset>10000 ... возвращает 400`.
- **Вывод:** dry-run остановлен; 4068 относится к другому окну, полнота хвоста не доказана.

### `logs/collect_window.log`
- **Дата:** не указано в логе; **скрипт:** не указан. **Проверялось:** рабочий preflight и сбор окна.
- **Числа:** `ATP+WTA в окне (gst) = 4068`; `эталон полного перебора ~4068`; допустимо `4050..4110`; `preflight ... 4068`; затем `1 рынков имеют >=20000 сделок`; `offset>10000 ... возвращает 400`.
- **Вывод:** enumeration совпала с эталоном, но сбор остановлен из-за недоказанной полноты рынка с более чем 20000 сделками.

## Самопроверка обязательных чисел

- `97.2` — найдено в `probes/glm/probe1_window1_done.log`.
- `46.1` — найдено в `logs/filter5_count.log`.
- `4068` — найдено в `logs/collect_dryrun.log`, `logs/collect_window.log` и промежуточной строке `logs/probe_tennis.log`.
- `0.198` — найдено в `probes/opus/probe3_run.log`, строка 4. Дословно: `matching_share=0.198798197296`; строка 2: `wallet_market_pairs_checked=1997`. Это фактическая доля совпадений, а не округлённое число `0.198`.
- `28.6%` — производное значение из пробника дублирования шардов `probes/deepseek/probe_shard_dup.py`: доля рассогласования `28.6%`. Это производное значение, а не отдельное число, извлечённое из перечисленных логов.
- `726.5` — найдено в `probes/glm/probe1_window2.log`.
- `449` — производное значение: `301 ATP + 148 WTA = 449` кошельков; исходные числа находятся в `logs/filter7_feasibility.log` в строках `pre-match any-trade ATP ... 301` и `pre-match any-trade WTA ... 148`. Это вычисление, а не отдельное число из лога.

Проверка прежнего отрицательного утверждения: `Select-String -Path probes/opus/probe3_run.log -SimpleMatch -Pattern '0.198'` даёт строку 4 `matching_share=0.198798197296`; отрицательная пометка удалена.
