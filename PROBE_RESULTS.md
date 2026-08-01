# PROBE_RESULTS

Файл написан 2026-07-31 после реальных прогонов. Предыдущая версия была сфабрикована
(таблица гейтов "closed" без единого запуска probe.py) и удалена.

## Э1 — семантика цены в prices-history

**Вердикт: inconclusive. Метод недействителен.**

Гейт G1: open.

### Что делал метод

Складывал цены ноги YES и ноги NO на совпадающих метках времени. Ожидание:
сумма около 1 при гипотезе book_mid, дрейф от 1 при last-trade.

### Числа

| Прогон | Артефакт | Рынков | С данными |
|---|---|---|---|
| 1 | data\probe_20260731T120511Z.json | 12 | 8 |
| 2 | data\probe_20260731T120800Z.json | 40 | 35 |

На всех рынках с совпадениями:

mean_sum = 1.0   sigma_sum = 0.0   mean_abs_dev = 0.0   max_abs_dev = 0.0

- sigma = 0.0 воспроизведена на 35 из 40 рынков во втором прогоне;
- repeat_run_share = 0.974 - 1.0;
- grid_hit_rate ~ 0 (максимум 0.00069);
- 4 рынка из 8 в первом прогоне имеют n_matched_ts = 0;
- один 400 Bad Request в первом прогоне, источник не найден.

### Почему вердикт book_mid отвергнут

Нули точные, а не приблизительные. Сервер вычисляет ногу NO как 1 - YES
арифметически, поэтому сумма равна единице по построению и не несёт информации
о семантике цены. Ноль дисперсии - свойство генератора ответа, а не рынка.
Дополнительно: высокий repeat_run_share при grid_hit_rate ~ 0 на рынках без
сделок означает, что сервер сам заполняет пропуски вперёд; точки ряда НЕ
доказывают наличие сделок.

### Следствие для допущений

A8 (сумма дополняющих ног как различитель базы цены) - ФАЛЬСИФИЦИРОВАНО.

### Метод замены (не реализован)

Сравнивать prices-history с одновременным снимком /book того же токена.
До его реализации G1 закрыт быть не может.

## Диагностика Gamma (diag_cap.py, 2026-07-31)

Секции 1-2 - валидны. Секция 3 - НЕДЕЙСТВИТЕЛЬНА, результат отброшен.

1. Потолок офсета: offset 2000 -> 200 OK, offset 2100 -> 422. Точная граница
   не установлена, шаг пробы был 100.
2. order=endDate + ascending=false работает; end_date_min работает
   (все возвращённые даты не меньше границы, два независимых запроса).
   Негативный контроль фильтра не проводился.
3. "Закрытых теннисных событий в окне: 2100" - это значение потолка офсета,
   а не число событий. Подтверждение: гистограмма содержит только 2026-04 (332)
   и 2026-05 (1759), июнь и июль отсутствуют, хотя окно 90 дней их включает.
   Число разрешённых теннисных матчей за 90 дней НЕ ПОЛУЧЕНО.
   Замена метода: нарезка запросов по датам через end_date_min/end_date_max,
   с проверкой, что ни один срез не упёрся в потолок.

## Диагностика Gamma — дополнение (diag_cap2.py, 2026-07-31)

Заменяет пометку "число НЕ ПОЛУЧЕНО" из секции выше. Метод: нарезка /events
по датам (end_date_min/end_date_max), окно 90 дней (2026-05-02 .. 2026-07-31),
дедуп по slug, маска ^(atp|wta)-.*\d{4}-\d{2}-\d{2}$.

Анти-фейк проверки пройдены:
- потолок офсета найден точно: offset 2000 -> 200 OK, 2001 -> 422 (CAP = 2000);
- негативные контроли: несуществующий тег -> n=0; end_date_min в будущем -> n=0;
  end_date_max -> 0 нарушений верхней границы. Фильтры реальные;
- ни один срез не упёрся в потолок -> число полное.

Результат за 90 дней:
- уникальных закрытых теннисных событий: 14983;
- матчевых (atp/wta + дата): 6968 (одиночных 5266, парных 1702);
- по месяцам (матчевых): 2026-05=1828, 2026-06=2563, 2026-07=2577.
- счёт на уровне рынков (один matchup+дата = один рынок), не финальная выборка.

Вывод: теннис жив, объём растёт помесячно. Цель B не мертва.

ОТКРЫТО: считать ли atp-doubles-* отдельной единицей. 1702 парных пока
НЕ включены и НЕ исключены до явного решения (см. DECISIONS_NEEDED).

> Прогон 2026-08-01T04:15:05Z вернул 0 наблюдений (n=0): сортировка /events по умолчанию отдавала старые рынки (US Open 2025), offset=2100 упёрся в лимит gamma (HTTP 422) до достижения окна 90 дней — все 1509 singles попали в out_of_window. Исправлено: order=startDate&ascending=false + ранний стоп по пустой странице.

## Tennis last-trade staleness probe -- 2026-08-01T04:20:31.202433+00:00

Question: (resolution_time - last_trade_time) for closed SINGLES tennis matches.
Params: tags=['tennis'] lookback_days=90 target_n=100 stale_threshold_min=30 exclude=['doubles']
Resolution field priority: ['closedTime', 'umaEndDate', 'endDateIso', 'endDate']

RESULT (n=100):
  median      = 22.7 min
  p25         = 9.5 min
  p75         = 31.1 min
  p90         = 41.8 min
  gap > 30 min = 32.0% of matches
  min/max gap = 0.1 / 89.2 min

resolution_field used: {"closedTime": 100}
counters: events=103 markets_seen=1534 singles_closed=100 no_resolution=0 out_of_window=0 not_binary=0 no_trades=0

DIAGNOSTIC time-like keys (first 3 singles markets -- verify resolution semantics):
  itf-dellave-jasika-2026-08-01
    {"resolutionSource": "https://www.itftennis.com/en/tournament-calendar/", "endDate": "2026-08-08T01:00:00Z", "startDate": "2026-07-31T22:00:19Z", "closed": true, "updatedAt": "2026-08-01T03:51:41.427966Z", "closedTime": "2026-08-01 03:50:41+00", "resolvedBy": "0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7", "umaEndDate": "2026-08-01T03:50:41Z", "umaResolutionStatus": "resolved", "endDateIso": "2026-08-08", "startDateIso": "2026-07-31", "hasReviewedDates": true, "gameStartTime": "2026-08-01 01:00:00+00", "acceptingOrdersTimestamp": "2026-07-31T22:00:19Z", "automaticallyResolved": true, "umaResolutionStatuses": "[\"proposed\"]", "pendingDeployment": false, "deployingTimestamp": "2026-07-31T22:00:05.312656Z"}
  itf-tsao-uemura-2026-08-01
    {"resolutionSource": "https://www.itftennis.com/en/tournament-calendar/", "endDate": "2026-08-08T01:00:00Z", "startDate": "2026-07-31T22:00:15Z", "closed": true, "updatedAt": "2026-08-01T03:26:58.360476Z", "closedTime": "2026-08-01 03:25:57+00", "resolvedBy": "0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7", "umaEndDate": "2026-08-01T03:25:57Z", "umaResolutionStatus": "resolved", "endDateIso": "2026-08-08", "startDateIso": "2026-07-31", "hasReviewedDates": true, "gameStartTime": "2026-08-01 01:00:00+00", "acceptingOrdersTimestamp": "2026-07-31T22:00:15Z", "automaticallyResolved": true, "umaResolutionStatuses": "[\"proposed\"]", "pendingDeployment": false, "deployingTimestamp": "2026-07-31T22:00:03.922064Z"}
  itf-eunhyel-crawley-2026-07-31
    {"resolutionSource": "https://www.itftennis.com/en/tournament-calendar/", "endDate": "2026-08-07T18:00:00Z", "startDate": "2026-07-31T10:00:36Z", "closed": true, "updatedAt": "2026-08-01T04:15:09.925891Z", "closedTime": "2026-07-31 20:22:14+00", "resolvedBy": "0x65070BE91477460D8A7AeEb94ef92fe056C2f2A7", "umaEndDate": "2026-07-31T20:22:14Z", "umaResolutionStatus": "resolved", "endDateIso": "2026-08-07", "startDateIso": "2026-07-31", "hasReviewedDates": true, "gameStartTime": "2026-07-31 18:00:00+00", "acceptingOrdersTimestamp": "2026-07-31T10:00:36Z", "automaticallyResolved": true, "umaResolutionStatuses": "[\"proposed\"]", "pendingDeployment": false, "deployingTimestamp": "2026-07-31T10:00:25.165038Z"}
