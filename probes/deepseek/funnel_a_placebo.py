#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""funnel_a_placebo.py -- ПЛАЦЕБО-КОНТРОЛЬ ФИЛЬТРА 5 (только счётчики).

Проверяет, объясняется ли число прошедших фильтр 5 (ATP 9 из 224, WTA 5 из 95)
реальным эффектом или заниженной стандартной ошибкой. Считаются ТОЛЬКО
счётчики прошедших; никаких gross_delta, cost, SE_cost, Стауффера, BH/BY --
это этап 2, его не касаемся.

Процедура, внутри тира, отдельно ATP и отдельно WTA (сиды 1..20):
  1. Кандидаты фильтра 1 (те же accumulate_pairs + filter1_pass из
     src/validate/funnel_a.py), n_i = число матчей кошелька. n_i не меняются.
  2. Пул = все предматчевые действительные пары кошелёк x матч тира (все
     строки артефакта с clv != None -- в точности то множество, из которого
     основной прогон строит вход фильтра 5).
  3. Схема B (ОСНОВНАЯ, вердикт по ней): глобальный шаффл пула сидом, раздача
     пар блоками n_1, n_2, ...; порядок раздачи блоков тоже рандомизируется
     тем же сидом; ни одна пара не используется дважды внутри прогона.
     Схема A (чувствительность): каждый синтетический кошелёк набирает n_i пар
     независимо без возвращения (random.sample из полного пула); пересечения
     между кошельками допустимы. Схема A вердикта НЕ определяет.
  4. Средний CLV, t и фильтр 5 считаются ТЕМ ЖЕ кодом, что и основной прогон:
     filter5_screen -> screen_one. Своей реализации фильтра здесь нет.
  5. 20 прогонов, сиды 1..20 (записаны в отчёт).

Контроль выбора схемы (решение владельца от 2026-08-06): если
sum(n_i) не мала относительно пула (по умолчанию доля >= 0.10),
схема переделывается; 19.7% (ATP) и 15.2% (WTA) малой долей не
являются, вердикт выносится по схеме B.

Выдача (подробно в FUNNEL_A_PLACEBO.md и placebo_run.log):
  - по каждому прогону схемы B и схемы A: число прошедших ATP и WTA;
  - среднее, медиана, максимум и полный список 20 значений по каждому тиру;
  - размер пула и сумма n_i по тирам;
  - по реальному прогону: распределение t среди 224 и среди 95 по децилям
    (D10..D90, min, max) и значения t у прошедших;
  - добор: хвосты реального t (t < -3 / t > 3, t < -2 / t > 2, t < -2.5 / t > 2.5),
    mean/std реального распределения t; разброс t плацебо B по всем синтетическим
    кошелькам всех 20 прогонов (D10..D90, min, max, mean, std);
  - вердикт по предобъявленному критерию (по схеме B).

Критерий (предобъявлен, не меняется):
  - mean_placebo >= 3  ->  SE_ZANIZHENA: сигнал списывается на заниженную
    ошибку, порог пересматривается;
  - mean_placebo < 1 И max_placebo < real_passes(тир)  ->  REAL_EFFECT;
  - иначе  ->  UNDECIDABLE (пишем как есть, никуда не подгоняем).

Адреса кошельков печатаются только в файл funnel_a_placebo_passers.json,
в консоль и в отчёт -- нет.

Запуск:            py -3.13 -u probes/deepseek/funnel_a_placebo.py
Тесты:             py -3.13 -u -m pytest tests/test_funnel_a_placebo.py -v
"""

from __future__ import annotations

import json
import math
import random
import statistics
import sys
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from src.validate.funnel_a import (  # noqa: E402
    ARTIFACT,
    TIERS,
    accumulate_pairs,
    filter1_pass,
    filter5_screen,
    iter_artifact_pairs,
)

# ------------------------------- константы -------------------------------
SEEDS = tuple(range(1, 21))          # 20 прогонов, сиды 1..20
PLACEBO_MEAN_UNDERESTIMATED = 3.0    # критерий: mean >= 3 -> SE занижена
PLACEBO_MEAN_REAL_EFFECT = 1.0       # критерий: mean < 1 (строго) -> кандидат на реальный эффект
SUM_NI_MAX_FRACTION = 0.10           # флаг: "сумма много меньше пула" = sum(n_i)/pool < 0.10

OUT_DIR = REPO_ROOT / "probes" / "deepseek"
OUT_MD = OUT_DIR / "FUNNEL_A_PLACEBO.md"
OUT_LOG = OUT_DIR / "placebo_run.log"
OUT_PASSERS = OUT_DIR / "funnel_a_placebo_passers.json"

_DECILE_POINTS = (10, 20, 30, 40, 50, 60, 70, 80, 90)


# ------------------------------- вывод в лог -------------------------------
class _Tee:
    """Дублирует вывод в stdout и в лог-файл (дословный лог прогона)."""

    def __init__(self, log_path: Path) -> None:
        self._stdout = sys.stdout
        self._file = open(log_path, "w", encoding="utf-8")

    def write(self, data: str) -> int:
        self._stdout.write(data)
        self._file.write(data)
        return len(data)

    def flush(self) -> None:
        self._stdout.flush()
        self._file.flush()

    def close(self) -> None:
        self._file.close()


# ------------------------------- пул и семплинг -----------------------------
def build_pool(f5: dict) -> dict[str, list[tuple[float, object, object]]]:
    """Пул пар тира = все строки артефакта с clv != None (как f5 основного прогона).

    Каждый элемент пула -- кортеж (clv, cond, day), где day -- ключ турнирного
    дня из accumulate_pairs (day_from_slug(slug) или cond). Реальная
    принадлежность пары кошельку здесь отбрасывается.
    """
    pool: dict[str, list[tuple[float, object, object]]] = {}
    for tier in TIERS:
        items: list[tuple[float, object, object]] = []
        for r in f5[tier].values():
            items.extend(zip(r["clv"], r["cond"], r["day"]))
        pool[tier] = items
    return pool


def sample_pairs(pool, n: int, rng: random.Random) -> list:
    """Схема A: n различных пар из пула (без возвращения, независимо по кошельку)."""
    if n < 0:
        raise ValueError("n_i must be >= 0")
    if n > len(pool):
        raise RuntimeError(
            "n_i=%d > размер пула=%d: независимый семплинг без возвращения "
            "невозможен; нужна схема B" % (n, len(pool)))
    return rng.sample(pool, n)


def build_synthetic_f5(pool, n_sizes: list[int], rng: random.Random, tag: str) -> dict:
    """f5 для синтетических кошельков схемы A: i-му кошельку ровно n_sizes[i] пар."""
    f5s: dict = {}
    for i, n in enumerate(n_sizes):
        items = sample_pairs(pool, n, rng)
        wid = "%s_%d" % (tag, i)
        f5s[wid] = {
            "clv": [it[0] for it in items],
            "cond": [it[1] for it in items],
            "day": [it[2] for it in items],
        }
    return f5s


def _screen_synthetic_a(pool_by_tier, n_by_tier: dict[str, list[int]], seed: int):
    """Схема A: собрать f5 синтетических кошельков, применить фильтр 5.

    Возвращает (passed, results) -- те же, что filter5_screen.
    """
    rng = random.Random(seed)
    f5s: dict = {t: {} for t in TIERS}
    for tier in TIERS:
        f5s[tier] = build_synthetic_f5(
            pool_by_tier[tier], n_by_tier[tier], rng, "s%02d_%s" % (seed, tier))
    f1p = {t: set(f5s[t]) for t in TIERS}
    return filter5_screen(f5s, f1p)


def run_one_seed(pool_by_tier, n_by_tier: dict[str, list[int]], seed: int) -> dict[str, int]:
    """Схема A: один плацебо-прогон (сид). Фильтр 5 -- тем же кодом (filter5_screen)."""
    passed, _ = _screen_synthetic_a(pool_by_tier, n_by_tier, seed)
    return {t: len(passed[t]) for t in TIERS}


# ------------------------------- схема B: блоки -----------------------------
def block_assign(pool: list, sizes: list[int], rng: random.Random) -> list[tuple[int, list]]:
    """Схема B: глобальный шаффл пула, раздача блоков n_i по порядку.

    Порядок раздачи блоков рандомизируется тем же rng (сидом): sizes
    перемешиваются перед нарезкой, чтобы первым кошелькам не доставался
    неистощённый пул, а последним -- остатки. Ни одна пара не используется
    дважды внутри прогона. Требует sum(sizes) <= len(pool).

    Возвращает список (n, items) в порядке раздачи.
    """
    total = sum(sizes)
    if total > len(pool):
        raise RuntimeError(
            "sum(n_i)=%d > размер пула=%d: схема B невозможна" % (total, len(pool)))
    shuffled = list(pool)
    rng.shuffle(shuffled)
    order = list(sizes)
    rng.shuffle(order)
    blocks: list[tuple[int, list]] = []
    cursor = 0
    for n in order:
        blocks.append((n, shuffled[cursor:cursor + n]))
        cursor += n
    return blocks


def _screen_synthetic_b(pool_by_tier, n_by_tier: dict[str, list[int]], seed: int):
    """Схема B: собрать f5 синтетических кошельков, применить фильтр 5.

    Возвращает (passed, results) -- те же, что filter5_screen.
    """
    rng = random.Random(seed)
    f5s: dict = {t: {} for t in TIERS}
    for tier in TIERS:
        blocks = block_assign(pool_by_tier[tier], n_by_tier[tier], rng)
        for i, (n, items) in enumerate(blocks):
            wid = "s%02d_%s_%d" % (seed, tier, i)
            f5s[tier][wid] = {
                "clv": [it[0] for it in items],
                "cond": [it[1] for it in items],
                "day": [it[2] for it in items],
            }
    f1p = {t: set(f5s[t]) for t in TIERS}
    return filter5_screen(f5s, f1p)


def run_one_seed_b(pool_by_tier, n_by_tier: dict[str, list[int]], seed: int) -> dict[str, int]:
    """Схема B: один плацебо-прогон (сид). Фильтр 5 -- тем же кодом (filter5_screen)."""
    passed, _ = _screen_synthetic_b(pool_by_tier, n_by_tier, seed)
    return {t: len(passed[t]) for t in TIERS}


# ------------------------------- статистика --------------------------------
def deciles(values: list[float]) -> dict:
    """Децили D10..D90 (numpy, линейная интерполяция) + min/max/неконечные."""
    if not values:
        return {"d": [None] * len(_DECILE_POINTS), "min": None, "max": None,
                "n_finite": 0, "n_inf": 0, "n_nan": 0, "n": 0}
    arr = np.asarray(values, dtype=float)
    finite = arr[np.isfinite(arr)]
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # inf в данных при линейной интерполяции
        d = [float(x) for x in np.percentile(arr, list(_DECILE_POINTS), method="linear")]
    return {
        "d": d,
        "min": float(arr.min()),
        "max": float(arr.max()),
        "n_finite": int(len(finite)),
        "n_inf": int(np.count_nonzero(np.isinf(arr))),
        "n_nan": int(np.count_nonzero(np.isnan(arr))),
        "n": int(len(arr)),
    }


def placebo_verdict(mean_placebo: float, max_placebo: int, real_passes: int) -> str:
    """Предобъявленный критерий плацебо. Порог не меняется после результата."""
    if mean_placebo >= PLACEBO_MEAN_UNDERESTIMATED:
        return "SE_ZANIZHENA"
    if mean_placebo < PLACEBO_MEAN_REAL_EFFECT and max_placebo < real_passes:
        return "REAL_EFFECT"
    return "UNDECIDABLE"


def tail_counts(values, threshold: float) -> tuple[int, int]:
    """(число t < -threshold, число t > threshold); равенство не в счёте."""
    lo = sum(1 for v in values if v < -threshold)
    hi = sum(1 for v in values if v > threshold)
    return lo, hi


def t_stats(values) -> dict:
    """mean/std распределения t по конечным значениям (numpy, ddof=0) + счёт неконечных."""
    finite = [v for v in values if math.isfinite(v)]
    arr = np.asarray(finite, dtype=float)
    mean = float(arr.mean()) if len(arr) else math.nan
    std = float(arr.std()) if len(arr) else math.nan
    n_inf = sum(1 for v in values if math.isinf(v))
    n_nan = len(values) - len(finite) - n_inf
    return {"mean": mean, "std": std, "n": len(values),
            "n_finite": len(finite), "n_inf": n_inf, "n_nan": n_nan}


def pooled_summary(values) -> dict:
    """Сводка разброса t по всем прогонам: децили, min/max, mean, std."""
    d = deciles(values)
    st = t_stats(values)
    return {"deciles": d, "mean": st["mean"], "std": st["std"],
            "n": st["n"], "n_finite": st["n_finite"], "n_inf": st["n_inf"], "n_nan": st["n_nan"]}


# ------------------------------- отчёт --------------------------------------
def _fmt_deciles(d: dict) -> str:
    if d["n"] == 0:
        return "нет данных"
    parts = ", ".join("%.4g" % x if x is not None else "-" for x in d["d"])
    return "D10..D90: %s | min %.4g | max %.4g | всего %d (конечных %d, inf %d, nan %d)" % (
        parts, d["min"], d["max"], d["n"], d["n_finite"], d["n_inf"], d["n_nan"])


def _render_runs(L: list[str], ctx: dict, scheme: str) -> None:
    """Таблица 20 прогонов + сводка для схемы scheme ("A"/"B")."""
    L.append("### Схема %s" % scheme)
    L.append("")
    if scheme == "A":
        L.append("Чувствительность: каждый синтетический кошелёк независимо набирает n_i пар "
                 "без возвращения из полного пула (пересечения между кошельками допустимы). "
                 "**Схема A вердикта НЕ определяет** -- вердикт по схеме B.")
    else:
        L.append("Глобальный шаффл пула сидом, раздача блоков n_i; порядок раздачи тоже "
                 "рандомизируется сидом; пара не используется дважды внутри прогона.")
    L.append("")
    L.append("| сид | ATP | WTA |")
    L.append("|---:|---:|---:|")
    for s, a, w in zip(ctx["seeds"], ctx[scheme]["counts"]["atp"], ctx[scheme]["counts"]["wta"]):
        L.append("| %d | %d | %d |" % (s, a, w))
    L.append("")
    L.append("| тир | полный список | среднее | медиана | максимум |")
    L.append("|---|---:|---:|---:|---:|")
    for t in TIERS:
        c = ctx[scheme]["summary"][t]
        L.append("| %s | %s | %.3f | %.3f | %d |" % (
            t.upper(), ", ".join(str(v) for v in ctx[scheme]["counts"][t]),
            c["mean"], c["median"], c["max"]))
    L.append("")


def render_md(ctx: dict) -> str:
    L: list[str] = []
    L.append("# Плацебо-контроль фильтра 5 (только счётчики)")
    L.append("")
    L.append("Окно валидации: `2026-02-01 .. 2026-04-28` (по `gameStartTime`), тиры ATP и WTA, внутри тира.")
    L.append("")
    L.append("## Факты прогона")
    L.append("")
    L.append("- дата/время (UTC): `%s`" % ctx["ts_utc"])
    L.append("- команда: `%s`" % ctx["cmdline"])
    L.append("- python: `%s`" % ctx["python"])
    L.append("- numpy: `%s`" % ctx["numpy"])
    L.append("- время счёта: `%.1f c`" % ctx["elapsed"])
    L.append("- сиды: `%s`" % ", ".join(str(s) for s in ctx["seeds"]))
    L.append("")
    L.append("Процедура: кандидаты фильтра 1 (n_i матчей на кошелёк, числа неизменны) -> "
             "синтетические кошельки получают ровно n_i пар из пула тира -> фильтр 5 "
             "применяется тем же кодом `filter5_screen`/`screen_one` из "
             "`src/validate/funnel_a.py`. Вердикт -- по схеме B (глобальный шаффл блоками).")
    L.append("")
    L.append("## Пул и n_i по тирам")
    L.append("")
    L.append("| тир | пул пар | сумма n_i | max n_i | сумма/пул | флаг sum(n_i) << пул |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for t in TIERS:
        r = ctx["pool_summary"][t]
        L.append("| %s | %d | %d | %d | %.5f | %s |" % (
            t.upper(), r["pool"], r["sum_ni"], r["max_ni"], r["fraction"],
            "да" if r["ok"] else "НЕТ"))
    L.append("")
    L.append("Флаг `sum(n_i) << пул` = сумма/пул < %.2f. По решению владельца от 2026-08-06: "
             "если флаг не выполнен, схема переделывается; доли 19.7%% (ATP) и 15.2%% (WTA) "
             "малой долей не являются, вердикт выносится по схеме B, схема A остаётся "
             "чувствительностью." % SUM_NI_MAX_FRACTION)
    L.append("")
    L.append("## Реальный прогон: распределение t и прошедшие")
    L.append("")
    for t in TIERS:
        L.append("### %s" % t.upper())
        L.append("")
        L.append("Кандидатов фильтра 1: `%d`, прошли фильтр 5: `%d`." % (
            ctx["real"][t]["n_candidates"], ctx["real"][t]["n_passed"]))
        L.append("")
        L.append("t-распределение по децилям: `%s`" % _fmt_deciles(ctx["real"][t]["deciles"]))
        L.append("")
        L.append("t у прошедших (n=%d): `%s`" % (
            len(ctx["real"][t]["passer_t"]),
            ", ".join("%.4g" % v for v in ctx["real"][t]["passer_t"])))
        L.append("")
    L.append("Адреса прошедших -- только в `probes/deepseek/funnel_a_placebo_passers.json`, "
             "не в консоли и не в этом отчёте.")
    L.append("")
    L.append("## Добор: хвосты реального t и разброс плацебо B")
    L.append("")
    L.append("Реальные t внутри тира, строго (`t < -порог` / `t > порог`, равенство не в счёте):")
    L.append("")
    L.append("| тир | t < -3 | t > 3 | t < -2 | t > 2 | t < -2.5 | t > 2.5 | mean | std |")
    L.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for t in TIERS:
        r = ctx["real_add"][t]
        L.append("| %s | %d | %d | %d | %d | %d | %d | %.4g | %.4g |" % (
            t.upper(), r["lt3"], r["gt3"], r["lt2"], r["gt2"], r["lt25"], r["gt25"],
            r["mean"], r["std"]))
    L.append("")
    L.append("mean/std по конечным t (numpy, `ddof=0`).")
    L.append("")
    L.append("Разброс t плацебо B: все синтетические кошельки всех 20 прогонов "
             "(ATP %d значений, WTA %d значений):" % (ctx["pb_pooled"]["atp"]["n"],
                                                      ctx["pb_pooled"]["wta"]["n"]))
    L.append("")
    L.append("| тир | n | D10..D90 | min | max | mean | std |")
    L.append("|---|---:|---|---:|---:|---:|---:|")
    for t in TIERS:
        ps = ctx["pb_pooled"][t]
        d = ps["deciles"]
        L.append("| %s | %d | %s | %.4g | %.4g | %.4g | %.4g |" % (
            t.upper(), ps["n"], ", ".join("%.4g" % x for x in d["d"]),
            d["min"], d["max"], ps["mean"], ps["std"]))
    L.append("")
    L.append("## 20 плацебо-прогонов (сиды 1..20)")
    L.append("")
    _render_runs(L, ctx, "B")
    _render_runs(L, ctx, "A")
    L.append("## Вердикт по предобъявленному критерию (по схеме B)")
    L.append("")
    L.append("Критерий: `mean >= 3` -> SE занижена, порог пересматривается; "
             "`mean < 1 И max < real_passes` -> эффект реальный; иначе -> неопределённый исход.")
    L.append("")
    L.append("| тир | real_passes | mean плацебо (B) | max плацебо (B) | вердикт |")
    L.append("|---|---:|---:|---:|---|")
    for t in TIERS:
        L.append("| %s | %d | %.3f | %d | %s |" % (
            t.upper(), ctx["real"][t]["n_passed"],
            ctx["B"]["summary"][t]["mean"], ctx["B"]["summary"][t]["max"],
            ctx["verdict"][t]))
    L.append("")
    L.append("Если вердикт `UNDECIDABLE` -- так и пишем, ни в одну сторону не подгоняем.")
    L.append("")
    return "\n".join(L)


# ------------------------------- главный прогон -----------------------------
def _run_scheme(scheme: str, pool_by_tier, n_by_tier) -> dict:
    """Прогон 20 сидов схемы scheme ("A"/"B"), возвращает счётчики, сводку и pooled_t.

    pooled_t[tier] -- t всех синтетических кошельков по всем 20 прогонам
    (для сравнения разброса реальных t с плацебо).
    """
    counts = {"atp": [], "wta": []}
    pooled_t = {t: [] for t in TIERS}
    for seed in SEEDS:
        if scheme == "B":
            passed, results = _screen_synthetic_b(pool_by_tier, n_by_tier, seed)
        else:
            passed, results = _screen_synthetic_a(pool_by_tier, n_by_tier, seed)
        counts["atp"].append(len(passed["atp"]))
        counts["wta"].append(len(passed["wta"]))
        for tier in TIERS:
            pooled_t[tier].extend(r.t for r in results[tier].values())
    summary = {}
    for tier in TIERS:
        c = counts[tier]
        summary[tier] = {
            "mean": statistics.mean(c),
            "median": statistics.median(c),
            "max": max(c),
        }
    return {"counts": counts, "summary": summary, "pooled_t": pooled_t}


def run() -> int:
    start = time.time()
    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    cmdline = " ".join(sys.argv)

    print("funnel_a_placebo.py -- плацебо-контроль фильтра 5 (счётчики)")
    print("ts_utc        : %s" % ts_utc)
    print("command       : %s" % cmdline)
    print("python        : %s" % sys.version.split()[0])
    print("numpy         : %s" % np.__version__)

    # 1) реальные данные: артефакт -> f1 (фильтр 1) и f5 (вход фильтра 5)
    print("\n-- чтение артефакта --")
    f1, f5, pairs_total = accumulate_pairs(iter_artifact_pairs(ARTIFACT))
    print("пар всего                          : %d" % pairs_total)
    print("пар ATP / WTA (строк с clv != None) : %d / %d" % (
        sum(len(v["clv"]) for v in f5["atp"].values()),
        sum(len(v["clv"]) for v in f5["wta"].values())))

    # 2) кандидаты фильтра 1, n_i
    f1p, f1d = filter1_pass(f1)
    n_by_tier: dict[str, list[int]] = {}
    for tier in TIERS:
        cand = sorted(f1p[tier])
        n_by_tier[tier] = [f1d[tier][w]["n_matches"] for w in cand]
    print("-- кандидаты фильтра 1 (n_i) --")
    for tier in TIERS:
        ns = n_by_tier[tier]
        print("%s: кандидатов %d, n_i min %d, max %d, сумма %d" % (
            tier.upper(), len(ns), min(ns), max(ns), sum(ns)))

    # 3) пул
    pool_by_tier = build_pool(f5)
    pool_summary = {}
    for tier in TIERS:
        pool = len(pool_by_tier[tier])
        s = sum(n_by_tier[tier])
        frac = s / pool if pool else math.inf
        pool_summary[tier] = {
            "pool": pool, "sum_ni": s, "max_ni": max(n_by_tier[tier], default=0),
            "fraction": frac,
            "ok": frac < SUM_NI_MAX_FRACTION,
        }
    print("-- пул и n_i --")
    for tier in TIERS:
        r = pool_summary[tier]
        print("%s: пул %d, сумма n_i %d, max n_i %d, сумма/пул %.5f, sum<<пул? %s" % (
            tier.upper(), r["pool"], r["sum_ni"], r["max_ni"], r["fraction"],
            "да" if r["ok"] else "НЕТ"))

    # 4) реальный фильтр 5: счётчики, t по децилям, t прошедших
    f5p, f5res = filter5_screen(f5, f1p)
    real = {}
    for tier in TIERS:
        cand = sorted(f1p[tier])
        ts_all = [f5res[tier][w].t for w in cand]
        passers = sorted(w for w in cand if f5res[tier][w].passed)
        real[tier] = {
            "n_candidates": len(cand),
            "n_passed": len(passers),
            "deciles": deciles(ts_all),
            "passer_t": [f5res[tier][w].t for w in passers],
            "passers": passers,
        }
    print("-- реальный фильтр 5 --")
    for tier in TIERS:
        r = real[tier]
        print("%s: кандидатов %d, прошли %d" % (tier.upper(), r["n_candidates"], r["n_passed"]))
        print("%s: t-децили %s" % (tier.upper(), _fmt_deciles(r["deciles"])))
        print("%s: t у прошедших (n=%d): %s" % (
            tier.upper(), len(r["passer_t"]),
            ", ".join("%.4g" % v for v in r["passer_t"])))

    # добор: хвосты реального t
    real_add = {}
    for tier in TIERS:
        ts = [f5res[tier][w].t for w in sorted(f1p[tier])]
        l3, g3 = tail_counts(ts, 3.0)
        l2, g2 = tail_counts(ts, 2.0)
        l25, g25 = tail_counts(ts, 2.5)
        st = t_stats(ts)
        real_add[tier] = {"lt3": l3, "gt3": g3, "lt2": l2, "gt2": g2,
                          "lt25": l25, "gt25": g25,
                          "mean": st["mean"], "std": st["std"],
                          "n": st["n"], "n_inf": st["n_inf"]}
    print("\n-- добор: хвосты реального t (строго t<-p и t>p, равенство не в счёте) --")
    for tier in TIERS:
        r = real_add[tier]
        print("%s: t<-3 %d | t>3 %d | t<-2 %d | t>2 %d | t<-2.5 %d | t>2.5 %d" % (
            tier.upper(), r["lt3"], r["gt3"], r["lt2"], r["gt2"], r["lt25"], r["gt25"]))
        print("%s: mean %.4g, std %.4g (n=%d, inf %d)" % (
            tier.upper(), r["mean"], r["std"], r["n"], r["n_inf"]))

    # 5) плацебо-прогоны: схема B (основная) и схема A (чувствительность)
    print("\n-- 20 плацебо-прогонов, СХЕМА B (глобальный шаффл блоками) --")
    sb = _run_scheme("B", pool_by_tier, n_by_tier)
    for seed, a, w in zip(SEEDS, sb["counts"]["atp"], sb["counts"]["wta"]):
        print("сид %2d : ATP %d, WTA %d" % (seed, a, w))
    for tier in TIERS:
        c = sb["counts"][tier]
        s = sb["summary"][tier]
        print("%s: полный список %s" % (tier.upper(), ", ".join(str(v) for v in c)))
        print("%s: среднее %.3f, медиана %.3f, максимум %d" % (
            tier.upper(), s["mean"], s["median"], s["max"]))

    print("\n-- 20 плацебо-прогонов, СХЕМА A (независимо по кошельку) -- чувствительность, вердикт не определяет")
    sa = _run_scheme("A", pool_by_tier, n_by_tier)
    for seed, a, w in zip(SEEDS, sa["counts"]["atp"], sa["counts"]["wta"]):
        print("сид %2d : ATP %d, WTA %d" % (seed, a, w))
    for tier in TIERS:
        c = sa["counts"][tier]
        s = sa["summary"][tier]
        print("%s: полный список %s" % (tier.upper(), ", ".join(str(v) for v in c)))
        print("%s: среднее %.3f, медиана %.3f, максимум %d" % (
            tier.upper(), s["mean"], s["median"], s["max"]))

    # добор: разброс t плацебо B по всем синтетическим кошелькам всех прогонов
    pb_pooled = {}
    for tier in TIERS:
        pb_pooled[tier] = pooled_summary(sb["pooled_t"][tier])
    print("\n-- добор: разброс t плацебо B (все синтетические кошельки, 20 прогонов) --")
    for tier in TIERS:
        ps = pb_pooled[tier]
        print("%s: n=%d, %s | mean %.4g, std %.4g" % (
            tier.upper(), ps["n"], _fmt_deciles(ps["deciles"]), ps["mean"], ps["std"]))

    # 6) вердикт по схеме B
    verdict = {}
    for tier in TIERS:
        verdict[tier] = placebo_verdict(
            sb["summary"][tier]["mean"], sb["summary"][tier]["max"], real[tier]["n_passed"])
    print("\n-- вердикт (по схеме B) --")
    for tier in TIERS:
        print("%s: real_passes %d, mean плацебо %.3f, max плацебо %d -> %s" % (
            tier.upper(), real[tier]["n_passed"], sb["summary"][tier]["mean"],
            sb["summary"][tier]["max"], verdict[tier]))

    # 7) отчёты на диск
    ctx = {
        "ts_utc": ts_utc,
        "cmdline": cmdline,
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "elapsed": time.time() - start,
        "seeds": SEEDS,
        "pool_summary": pool_summary,
        "real": {t: {k: v for k, v in real[t].items() if k != "passers"} for t in TIERS},
        "real_add": real_add,
        "pb_pooled": pb_pooled,
        "B": sb,
        "A": sa,
        "verdict": verdict,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(render_md(ctx) + "\n")
    print("\nзаписан отчёт: %s" % OUT_MD)

    payload = {
        "window": {"start": "2026-02-01", "end_excl": "2026-04-28"},
        "note": "Реальные кошельки, прошедшие фильтр 5 внутри тира. Адреса только здесь.",
        "atp": [{"wallet": w, "t": f5res["atp"][w].t,
                 "mean_clv": f5res["atp"][w].mean_clv,
                 "n_matches": f5res["atp"][w].n_matches} for w in real["atp"]["passers"]],
        "wta": [{"wallet": w, "t": f5res["wta"][w].t,
                 "mean_clv": f5res["wta"][w].mean_clv,
                 "n_matches": f5res["wta"][w].n_matches} for w in real["wta"]["passers"]],
    }
    with open(OUT_PASSERS, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    print("записан список прошедших (адреса): %s" % OUT_PASSERS)

    print("\nвремя прогона: %.1f c" % (time.time() - start))
    return 0


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tee = _Tee(OUT_LOG)
    try:
        sys.stdout = tee
        return run()
    finally:
        sys.stdout = tee._stdout
        tee.close()


if __name__ == "__main__":
    raise SystemExit(main())
