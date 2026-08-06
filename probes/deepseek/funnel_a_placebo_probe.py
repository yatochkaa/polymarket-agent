#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""funnel_a_placebo_probe.py -- ФАКТЫ ДАННЫХ ДЛЯ ДИЗАЙНА ПЛАЦЕБО-КОНТРОЛЯ ФИЛЬТРА 5.

Проба не считает плацебо. Она устанавливает факты о замороженном артефакте
data/collect_window_2026-02-01_2026-04-28.json, от которых зависит дизайн
probes/deepseek/funnel_a_placebo.py:

  F1. Структура строк артефакта (поля pair), на примере первых пар ATP и WTA.
  F2. Число пар по тирам (ожидается ATP 307151 / WTA 133697 -- сверка с
      probes/deepseek/funnel_a_run.log).
  F3. Уникальность пары (wallet, cond): сколько строк с повторным (tier, wallet,
      cond). Если повторы есть -- пул пар надо строить осторожно.
  F4. Доля строк с clv = None. В основном прогоне f5 (вход фильтра 5) строится
      ТОЛЬКО из строк с clv != None (accumulate_pairs), поэтому пул должен
      совпадать с этим множеством.
  F5. Доля строк, у которых по слагу не извлекается дата турнирного дня
      (day_from_slug = None): такие строки в f5 получают ключ дня = cond.
  F6. Кандидаты фильтра 1 (та же функция filter1_pass), их число
      (ожидается ATP 224 / WTA 95), распределение n_i (матчей на кошелёк):
      min / max / сумма.
  F7. Выполнимость выборки без возвращения:
        - вариант A (независимо по каждому синтетическому кошельку): всегда
          выполним, если n_i <= размер пула для каждого n_i;
        - вариант B (глобально, без повторного использования пар между
          синтетическими кошельками): нужен пул >= sum(n_i).
  F8. Для кандидатов фильтра 1: len(clv) == n_matches (число строк с clv
      совпадает с числом матчей). Проверяет, что "n_i пар" == "n_i значений clv".

Код фильтра 1 и чтение артефакта -- те же функции src/validate/funnel_a.py
(accumulate_pairs, filter1_pass, iter_artifact_pairs), чтобы факты были
согласованы с основным прогоном. Свою реализацию фильтра проба не пишет.

Запуск (терминал):
  py -3.13 -u probes/deepseek/funnel_a_placebo_probe.py
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.validate.funnel_a import (  # noqa: E402
    ARTIFACT,
    TIERS,
    accumulate_pairs,
    day_from_slug,
    filter1_pass,
    iter_artifact_pairs,
)


def main() -> int:
    start = time.time()
    print("funnel_a_placebo_probe.py -- факты данных для дизайна плацебо")
    print("ts_utc        : %s" % datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
    print("python        : %s" % sys.version.split()[0])

    # F1: образец строк артефакта
    print("\n-- F1: структура строк артефакта (первые 2 пары ATP, 2 WTA) --")
    sample_atp = []
    sample_wta = []
    with open(ARTIFACT, "rb") as f:
        import ijson
        for obj in ijson.items(f, "pairs.item"):
            t = obj.get("tier")
            if t == "atp" and len(sample_atp) < 2:
                sample_atp.append(obj)
            elif t == "wta" and len(sample_wta) < 2:
                sample_wta.append(obj)
            if len(sample_atp) >= 2 and len(sample_wta) >= 2:
                break
    for obj in sample_atp + sample_wta:
        print("  %s: %s" % (obj.get("tier"), sorted(obj.keys())))

    # F2-F8: один проход по артефакту
    print("\n-- F2-F8: проход по артефакту --")
    f1, f5, pairs_total = accumulate_pairs(iter_artifact_pairs(ARTIFACT))
    print("пар всего                          : %d" % pairs_total)
    pool_len = {t: 0 for t in TIERS}        # строки с clv != None
    dup_pairs = {t: 0 for t in TIERS}       # повторные (wallet, cond)
    seen = {t: set() for t in TIERS}
    clv_none = {t: 0 for t in TIERS}
    day_none = {t: 0 for t in TIERS}
    for p in iter_artifact_pairs(ARTIFACT):
        t = p.get("tier")
        if t not in TIERS:
            continue
        key = ((p.get("wallet") or "").strip().lower(), p.get("cond"))
        if key in seen[t]:
            dup_pairs[t] += 1
        else:
            seen[t].add(key)
        clv = p.get("clv")
        if clv is None:
            clv_none[t] += 1
        else:
            pool_len[t] += 1
        if day_from_slug(p.get("slug") or "") is None:
            day_none[t] += 1

    for t in TIERS:
        n_w = len(f1[t])
        n_matches_w = sum(len(v["conds"]) for v in f1[t].values())
        print("-- %s --" % t.upper())
        print("  пар в артефакте (строк)        : %d" % n_matches_w)
        print("  строк с clv != None (пул)      : %d" % pool_len[t])
        print("  повторных (wallet, cond)       : %d" % dup_pairs[t])
        print("  строк с clv == None            : %d" % clv_none[t])
        print("  строк без даты в слаге         : %d" % day_none[t])
        print("  кошельков с >=1 парой          : %d" % n_w)

    # F6: кандидаты фильтра 1 и n_i
    print("\n-- F6: кандидаты фильтра 1 (n_i) --")
    f1p, f1d = filter1_pass(f1)
    for t in TIERS:
        cand = sorted(f1p[t])
        ns = [f1d[t][w]["n_matches"] for w in cand]
        print("%s: кандидатов %d, n_i min %d, max %d, сумма %d, среднее %.2f" % (
            t.upper(), len(cand), min(ns), max(ns), sum(ns), sum(ns) / len(ns) if ns else 0.0))

    # F7: выполнимость
    print("\n-- F7: выполнимость выборки без возвращения --")
    for t in TIERS:
        cand = sorted(f1p[t])
        ns = [f1d[t][w]["n_matches"] for w in cand]
        pool = pool_len[t]
        print("%s: пул %d, max(n_i) %d, sum(n_i) %d, пул >= max(n_i)? %s, пул >= sum(n_i)? %s" % (
            t.upper(), pool, max(ns, default=0), sum(ns),
            pool >= max(ns, default=0), pool >= sum(ns)))

    # F8: len(clv) == n_matches для кандидатов
    print("\n-- F8: len(clv) == n_matches у кандидатов --")
    for t in TIERS:
        bad = 0
        worst = (0, 0)
        for w in f1p[t]:
            n = f1d[t][w]["n_matches"]
            r = f5[t].get(w)
            k = len(r["clv"]) if r is not None else 0
            if k != n:
                bad += 1
                worst = max(worst, (abs(k - n), n))
        print("%s: несовпадений %d, худшее (|delta|, n_matches) = %s" % (t.upper(), bad, worst))

    print("\nвремя пробы: %.1f c" % (time.time() - start))
    print("вердикт пробы: факты напечатаны, дизайн плацебо решает человек")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
