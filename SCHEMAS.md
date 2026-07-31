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
