# GLM: M* при N_min=5 по `probes/opus/spread_estimator_nmin.csv`

Только чтение CSV; пересчёта и перезаписи нет. Интерпретатор: Python 3.13 + pandas/numpy. Запуск с флагом `-u`.

## Вывод консоли (дословно)

```
GLM: mstar_nmin5 -- read-only анализ spread_estimator_nmin.csv при N_min=5
python: 3.13.5
pandas: 2.3.1 | numpy: 2.3.1
CSV: C:\Users\awf\Desktop\test\probes\opus\spread_estimator_nmin.csv
CSV exists: True

rows: 449342
columns (24):
  - conditionId
  - proxyWallet
  - tier
  - decisive
  - sz0
  - sz1
  - n_buy0_60
  - n_sell0_60
  - n_buy1_60
  - n_sell1_60
  - n_buy0_10
  - n_sell0_10
  - n_buy1_10
  - n_sell1_10
  - spread_cost_point_raw_60
  - spread_cost_point_60
  - spread_cost_point_raw_10
  - spread_cost_point_10
  - measurable_60_N5
  - measurable_60_N10
  - measurable_60_N20
  - mstar_N5
  - mstar_N10
  - mstar_N20

=== 1) M* при N_min=5 (множество mstar_N5 == 1) ===
|M*| = 27282
median(spread_cost_point_60) = 0.005096   (n=27282, NaN=0)
median(spread_cost_point_10) = 0.005000   (n=27282, NaN=0)
ratio = median60 / median10 = 1.019200

=== 2) Guard на различие пулов при N_min=5 ===
колонок с размерами пулов (как единого значения пула на пару/окно) НЕТ.
колонок со словом 'pool': нет
колонок вида pool_size_* / n_pool_*: нет

Реальные имена колонок файла (24):
  - conditionId
  - proxyWallet
  - tier
  - decisive
  - sz0
  - sz1
  - n_buy0_60
  - n_sell0_60
  - n_buy1_60
  - n_sell1_60
  - n_buy0_10
  - n_sell0_10
  - n_buy1_10
  - n_sell1_10
  - spread_cost_point_raw_60
  - spread_cost_point_60
  - spread_cost_point_raw_10
  - spread_cost_point_10
  - measurable_60_N5
  - measurable_60_N10
  - measurable_60_N20
  - mstar_N5
  - mstar_N10
  - mstar_N20

Примечание: в файле есть только попарные подсчёты сделок пула по токенам/сторонам:
  D=60: ['n_buy0_60', 'n_sell0_60', 'n_buy1_60', 'n_sell1_60']
  D=10: ['n_buy0_10', 'n_sell0_10', 'n_buy1_10', 'n_sell1_10']
Это компоненты размера пула (n_buy + n_sell по активным токенам), но
готовой колонки 'размер пула на окно' нет, а 'полоса между окнами'
количественно не определена. Согласно инструкции числа guard'а не
выдумываются и не оцениваются.

```
