#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# probes/glm/mstar_nmin5.py
# GLM: read-only analysis of probes/opus/spread_estimator_nmin.csv at N_min=5.
#
# Ничего не пересчитывает и не перезаписывает. CSV только читается.
# Интерпретатор: Python 3.13 + pandas/numpy (тот же, которым считался CSV).
# Запуск: python -u probes/glm/mstar_nmin5.py
#
# Выход: probes/glm/mstar_nmin5.md (с дублированием вывода консоли дословно).

import os
import sys

# --- fail-fast: если pandas/numpy не импортируются - останавливаемся,
#     ничего не переустанавливаем и не переписываем на чистый Python. ---
try:
    import numpy as np
    import pandas as pd
except Exception as e:  # pragma: no cover
    print("STOP: pandas/numpy import failed: %r" % (e,), flush=True)
    sys.exit(3)

HERE = os.path.dirname(os.path.abspath(__file__))
CSV = os.path.normpath(os.path.join(HERE, "..", "opus", "spread_estimator_nmin.csv"))
MD = os.path.join(HERE, "mstar_nmin5.md")

L = []  # captured console lines for verbatim MD duplication


def out(s=""):
    print(s, flush=True)
    L.append(s)


out("GLM: mstar_nmin5 -- read-only анализ spread_estimator_nmin.csv при N_min=5")
out("python: %s" % sys.version.split()[0])
out("pandas: %s | numpy: %s" % (pd.__version__, np.__version__))
out("CSV: %s" % CSV)
out("CSV exists: %s" % os.path.exists(CSV))
out("")

# --- read-only load ---
df = pd.read_csv(CSV)
out("rows: %d" % len(df))
out("columns (%d):" % len(df.columns))
for c in df.columns:
    out("  - %s" % c)
out("")

N = 5

# ===========================================================================
# 1) M* при N_min=5: пары, измеримые одновременно в окне D=10 и в окне D=60.
#    В CSV это колонка mstar_N5 (= measurable_60_N5 & measurable_10_N5,
#    см. spread_estimator_nmin.py, сборка m[N][1]).
# ===========================================================================
mstar_col = "mstar_N%d" % N
if mstar_col not in df.columns:
    out("STOP: колонка %s отсутствует" % mstar_col)
    sys.exit(2)

m = df[mstar_col].astype(int) == 1
n_mstar = int(m.sum())

out("=== 1) M* при N_min=%d (множество %s == 1) ===" % (N, mstar_col))
out("|M*| = %d" % n_mstar)

spc60 = df.loc[m, "spread_cost_point_60"]
spc10 = df.loc[m, "spread_cost_point_10"]
nan60 = int(spc60.isna().sum())
nan10 = int(spc10.isna().sum())
med60 = float(spc60.median())
med10 = float(spc10.median())
ratio = (med60 / med10) if med10 != 0 else float("nan")

out("median(spread_cost_point_60) = %.6f   (n=%d, NaN=%d)"
    % (med60, n_mstar, nan60))
out("median(spread_cost_point_10) = %.6f   (n=%d, NaN=%d)"
    % (med10, n_mstar, nan10))
out("ratio = median60 / median10 = %.6f" % ratio)
out("")

# ===========================================================================
# 2) Guard на различие пулов при N_min=5.
#    Готовой колонки "размер пула" (единого значения на пару/окно) в файле НЕТ.
#    Есть только попарные подсчёты сделок пула по токенам/сторонам
#    (n_buy/n_sell per token per window). Согласно инструкции числа не
#    выдумываются и не оцениваются -> сообщаем "колонок нет" + список колонок.
# ===========================================================================
out("=== 2) Guard на различие пулов при N_min=%d ===" % N)

pool_kw = [c for c in df.columns if "pool" in c.lower()]
size_kw = [c for c in df.columns if c.lower().startswith("pool_size")
           or c.lower().startswith("n_pool")]

out("колонок с размерами пулов (как единого значения пула на пару/окно) НЕТ.")
out("колонок со словом 'pool': %s" % (pool_kw if pool_kw else "нет"))
out("колонок вида pool_size_* / n_pool_*: %s" % (size_kw if size_kw else "нет"))
out("")

out("Реальные имена колонок файла (%d):" % len(df.columns))
for c in df.columns:
    out("  - %s" % c)
out("")

per_token_counts = {
    "60": ["n_buy0_60", "n_sell0_60", "n_buy1_60", "n_sell1_60"],
    "10": ["n_buy0_10", "n_sell0_10", "n_buy1_10", "n_sell1_10"],
}
out("Примечание: в файле есть только попарные подсчёты сделок пула по токенам/сторонам:")
for win in ("60", "10"):
    present = [c for c in per_token_counts[win] if c in df.columns]
    out("  D=%s: %s" % (win, present))
out("Это компоненты размера пула (n_buy + n_sell по активным токенам), но")
out("готовой колонки 'размер пула на окно' нет, а 'полоса между окнами'")
out("количественно не определена. Согласно инструкции числа guard'а не")
out("выдумываются и не оцениваются.")
out("")

# --- write MD (console output duplicated verbatim) ---
with open(MD, "w", encoding="utf-8") as f:
    f.write("# GLM: M* при N_min=5 по `probes/opus/spread_estimator_nmin.csv`\n\n")
    f.write("Только чтение CSV; пересчёта и перезаписи нет. "
            "Интерпретатор: Python 3.13 + pandas/numpy. Запуск с флагом `-u`.\n\n")
    f.write("## Вывод консоли (дословно)\n\n")
    f.write("```\n")
    f.write("\n".join(L) + "\n")
    f.write("```\n")

out("")
out("MD written: %s" % MD)