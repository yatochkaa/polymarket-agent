# Перепроверка spread_estimator_nmin.csv (read-only)

Режим: только чтение. Оценщик `spread_estimator_nmin.py` не запускался, CSV не
перезаписывался, `nmin_recompute.md` не трогался. Ничего не коммитилось.

## Артефакт

| поле | значение |
|---|---|
| файл | `probes/opus/spread_estimator_nmin.csv` |
| размер | 79 447 259 байт |
| mtime | 2026-08-06 05:49:35 +03:00 |
| sha256 | `b19f17b805967141347ed7cd061139dd4461494c3ba23f9dd4b3b58d842fc877` |
| окружение | Python 3.13.5, pandas 2.3.1, numpy 2.3.1, запуск `py -3.13 -u` |

Импорт pandas/numpy прошёл (в 3.13; в дефолтном интерпретаторе оболочки 3.11
pandas отсутствует — использован явно 3.13, как задано).

## Ответы

**1. Строк: 449 342. Колонок: 24.**
`pd.read_csv(...)` → `len(df)` и `list(df.columns)`; полный список:
`conditionId, proxyWallet, tier, decisive, sz0, sz1, n_buy0_60, n_sell0_60,
n_buy1_60, n_sell1_60, n_buy0_10, n_sell0_10, n_buy1_10, n_sell1_10,
spread_cost_point_raw_60, spread_cost_point_60, spread_cost_point_raw_10,
spread_cost_point_10, measurable_60_N5, measurable_60_N10, measurable_60_N20,
mstar_N5, mstar_N10, mstar_N20`.

**2. 12 779 пар измеримо.**
Счёт `measurable_60_N20 == 1` (value_counts: 0 → 436 563, 1 → 12 779); внутри
подмножества все 12 779 значений `spread_cost_point_60` непустые.

**3. По тирам на подмножестве `measurable_60_N20 == 1`:**

| тир | пар | медиана `spread_cost_point_60` | p90 |
|---|---|---|---|
| atp | 9 317 | 0.005000 | 0.049567 |
| wta | 3 462 | 0.004630 | 0.050961 |

Получено `groupby(tier)` внутри маски N20, `median()` и `np.percentile(v, 90)`
(линейная интерполяция; `Series.quantile(0.9)` даёт то же самое).

**4. 0 строк.**
Поэлементное сравнение `spread_cost_point_60` с `raw_60.clip(lower=0)`: 250 302
строки, где оба поля непусты, 199 040, где оба NaN, 0 расхождений по шаблону
NaN, 0 строгих неравенств и 0 отклонений выше 1e-12. Все 35 254 отрицательных
`raw_60` отображены ровно в 0.0; минимум `spread_cost_point_60` = 0.0.

**5. 0.311135 (31.1135%), то есть 3 976 из 12 779.**
`(raw_60 < 0).sum()` на маске `measurable_60_N20 == 1`; знаменатель — все 12 779
измеримых пар, у всех `raw_60` непустой, так что деление на строки и на непустые
совпадает.

**6. Нет.**
Колонок с «120» в имени нет; единственные оконные суффиксы в схеме — `_10` и
`_60`. Оценки спреда для D = 120 в данных нет, медиану считать не из чего.

**7. Нет, не воспроизводится.**
Прогон по 126 подмножествам (базы: все строки, непустой `raw_60`, непустой
`raw_10`, `measurable_60_N5/N10/N20`, `mstar_N5/N10/N20`; срезы: без среза, atp,
wta, decisive=0, decisive=1, sz0>0, sz1>0) × 2 колонки (`raw_60`, `raw_10`) ×
2 знаменателя (все строки среза / непустые значения). Отрицательных ровно 1 951
не даёт ни одно подмножество; ближайшие — 1 966 (`measurable_60_N20 & sz0>0`,
`raw_60`) и 1 916 (`mstar_N10 & sz0>0`, `raw_60`). Доля ≈15.3% встречается один
раз и с другим счётом: 5 223 из 34 071 = 15.3298% (`measurable_60_N5 & sz1>0`,
`raw_10`) — то есть пара «1 951 и 15.3%» одновременно не выполняется нигде.
Подстановка знаменателя 1951/0.153 = 12 751.6 близка по размеру к
`measurable_60_N20` (12 779), но там отрицательных 3 976 (31.11%), а не 1 951 —
похоже, старая цифра относилась к другому артефакту либо к другой формуле, в
этом файле её основания нет.

## Дословный вывод консоли

```
python: 3.13.5 | pandas: 2.3.1 | numpy: 2.3.1
file: probes/opus/spread_estimator_nmin.csv | size_bytes: 79447259 | mtime: 2026-08-06 05:49:35.013027668+03:00
sha256: b19f17b805967141347ed7cd061139dd4461494c3ba23f9dd4b3b58d842fc877

### Q1
rows: 449342 | cols: 24
columns: ['conditionId', 'proxyWallet', 'tier', 'decisive', 'sz0', 'sz1', 'n_buy0_60', 'n_sell0_60', 'n_buy1_60', 'n_sell1_60', 'n_buy0_10', 'n_sell0_10', 'n_buy1_10', 'n_sell1_10', 'spread_cost_point_raw_60', 'spread_cost_point_60', 'spread_cost_point_raw_10', 'spread_cost_point_10', 'measurable_60_N5', 'measurable_60_N10', 'measurable_60_N20', 'mstar_N5', 'mstar_N10', 'mstar_N20']

### Q2
measurable_60_N20 value_counts: {0: 436563, 1: 12779}
measurable pairs at N_min=20, D=60: 12779
non-null spread_cost_point_60 inside that subset: 12779

### Q3  (subset measurable_60_N20==1)
tier=atp: n_pairs=9317 median=0.005 p90=0.049567
tier=wta: n_pairs=3462 median=0.00463 p90=0.0509613

### Q4
rows both non-null: 250302 | rows both NaN: 199040 | NaN-pattern mismatches: 0
rows with spread_cost_point_60 != max(raw_60,0)  [exact !=]: 0
rows with |diff| > 1e-12: 0
sanity: negatives in raw_60 = 35254 -> all mapped to exactly 0.0: 35254
min point_60: 0.0 | max point_60: 0.37

### Q5
negatives=3976 / measurable=12779 = 0.311135 = 31.1135%   (raw_60 non-null inside subset: 12779)

### Q6
columns containing '120': []
numeric window suffixes present: ['10', '60']

### Q7  sweep for neg==1951 or share==15.3%
subsets scanned: 126
subsets with negative count exactly 1951: NONE
subsets whose negative share rounds to 15.3% (any denominator):
   measurable_60_N5+sz1>0 / spread_cost_point_raw_10  denom=rows  neg=5223  denom_n=34071  pct=15.3298
--> requirement is neg==1951 AND share==15.3% simultaneously: matches = NONE
closest negative counts to 1951:
   measurable_60_N20+sz0>0 / spread_cost_point_raw_60: neg=1966 nonnull=7088 rows=7088
   mstar_N10+sz0>0 / spread_cost_point_raw_60: neg=1916 nonnull=6561 rows=6561
   measurable_60_N20+sz1>0 / spread_cost_point_raw_10: neg=1829 nonnull=5696 rows=5953
   mstar_N10+sz0>0 / spread_cost_point_raw_10: neg=1818 nonnull=6561 rows=6561
   mstar_N20 / spread_cost_point_raw_60: neg=1775 nonnull=5189 rows=5189
   measurable_60_N20+sz1>0 / spread_cost_point_raw_60: neg=2136 nonnull=5953 rows=5953
implied denominator for 1951 at 15.3%: 1951/0.153 = 12751.6 | nearest real subset size: measurable_60_N20 = 12779 -> its actual negatives: 3976 (31.11%)
negatives in clipped columns (should be 0): 0 0
```

## Побочные наблюдения (не запрошено, для контекста)

- Уникальных `conditionId` 4 064, `proxyWallet` 37 190, дублей пары
  (conditionId, proxyWallet) нет.
- Отрицательных `raw_60` по всему файлу 35 254 (14.08% от 250 302 непустых).
- Измеримость по другим порогам: N5 → 71 550, N10 → 30 357, N20 → 12 779;
  `mstar`: 27 282 / 12 075 / 5 189.
- Доля отрицательных растёт с порогом (N5 18.18% → N10 23.39% → N20 31.11%) и
  выше в wta (42.00% при N20) чем в atp (27.07%).
