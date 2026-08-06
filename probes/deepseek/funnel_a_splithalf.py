#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""funnel_a_splithalf.py -- РАСЩЕПЛЕНИЕ ПОПОЛАМ ПО ВРЕМЕНИ (split-half CLV).

Проверяет, чем объясняется ширина распределения t фильтра 5 (реальные
std 1.674/1.66 против плацебо 0.892/0.847): настоящей разницей между
кошельками или заниженной ошибкой на уровне кошелька.

Данные те же, новых не требуется: замороженный артефакт
data/collect_window_2026-02-01_2026-04-28.json, один проход.

Метод (предобъявлен, до прогона):
  Окно [WIN_START, WIN_END_EXCL) делится по дате матча (gst) на две
  половины РАВНОЙ длительности: mid = start + (end - start)/2.
  Половины: первая gst < mid, вторая gst >= mid (равенство границы --
  во вторую). Пары вне окна или без gst в половины не попадают
  (счётчик unplaced в отчёте).
  Для КАЖДОГО кандидата фильтра 1 средний CLV и t считаются отдельно
  по каждой половине ТЕМ ЖЕ кодом, что и в основном прогоне: screen_one
  из src/validate/funnel_a.py (mean_clv = сумма/число, t = mean/SE_edge).
  Кандидат попадает в выборку корреляции, если в ОБЕИХ половинах
  n >= MIN_MATCHES_HALF. Порог одинаков для реальных и синтетических
  кошельков и после прогона не меняется.

  MIN_MATCHES_HALF = 50: половина порога фильтра 1 (100 матчей на всё
  окно из 86 дней). Половина окна -- 43 дня, 50 матчей за половину --
  та же плотность, что 100 за всё окно.

Правило решения (объявлено ДО прогона, порог 0.15 не меняется):
  Состояние тира по корреляции Пирсона между половинами:
    SUPPORT   : r >= 0.15 И p < 0.05;
    NULL      : p >= 0.05 (статистически неотличима от нуля);
    AMBIGUOUS : p < 0.05, но r < 0.15.
  Итог:
    GO        : ОБА тира SUPPORT -> разброс настоящий, ширина отражает
                реальную разницу между кошельками, идём на этап 2;
    NO-GO     : ОБА тира NULL -> ширина есть артефакт ошибки,
                кандидаты не подтверждены, этап 2 не начинаем;
    UNDECIDABLE: иначе (в т.ч. смешанный случай), ничего не подгоняем.
  Решающая статистика -- корреляция Пирсона; Спирмен приводится как
  робастность. p-значение двустороннее, по t-распределению (df = n-2).

Выдача (просто числа, вне правила ничего не интерпретируется):
  1. Пирсон и Спирмен между половинами по всем кандидатам (n и p);
  2. та же корреляция на синтетических кошельках плацебо схемы B
     (калибровка; ожидание -- около нуля), 20 сидов 1..20, механизм
     block_assign тот же, что в funnel_a_placebo.py;
  3. сколько кандидатов имеют достаточно матчей в обеих половинах и
     сколько выбыло из-за нехватки (с разбивкой по стороне);
  4. по 9 прошедшим ATP и 5 WTA: средний CLV и t в первой и второй
     половинах таблицей; адреса только в splithalf_passers.json;
  5. среди 9 и 5 -- у скольких средний CLV положителен во ВТОРОЙ
     половине.

Запуск:  py -3.13 -u probes/deepseek/funnel_a_splithalf.py
Тесты:   py -3.13 -u -m pytest tests/test_funnel_a_splithalf.py -v
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np  # noqa: E402

from probes.deepseek.funnel_a_placebo import SEEDS, block_assign  # noqa: E402
from src.validate.funnel_a import (  # noqa: E402
    ARTIFACT,
    TIERS,
    WIN_END_EXCL,
    WIN_START,
    accumulate_pairs,
    day_from_slug,
    filter1_pass,
    filter5_screen,
    iter_artifact_pairs,
    screen_one,
)

# ------------------------------- константы -------------------------------
MIN_MATCHES_HALF = 50          # половина порога фильтра 1 (100/2)
SPLIT_GO_MIN_R = 0.15          # правило: r >= 0.15 (включительно)
SPLIT_ALPHA = 0.05             # правило: p < 0.05 (строго)

OUT_DIR = REPO_ROOT / "probes" / "deepseek"
OUT_MD = OUT_DIR / "FUNNEL_A_SPLITHALF.md"
OUT_LOG = OUT_DIR / "splithalf_run.log"
OUT_PASSERS = OUT_DIR / "splithalf_passers.json"

_BETACF_MAX_ITER = 200
_BETACF_EPS = 3.0e-14
_BETACF_FPMIN = 1.0e-300


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


# ------------------------------- окно и границы ----------------------------
def _utc_ts(day_str: str) -> float:
    """Эпоха-секунды UTC для календарной даты YYYY-MM-DD 00:00."""
    return datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def window_bounds() -> tuple[float, float, float]:
    """(start_ts, end_ts, mid_ts): середина по длительности, половины равные."""
    start = _utc_ts(WIN_START)
    end = _utc_ts(WIN_END_EXCL)
    mid = (start + end) / 2.0
    return start, end, mid


# ------------------------------- накопление --------------------------------
def accumulate_pairs_with_gst(pairs):
    """Как accumulate_pairs из funnel_a.py, но f5 дополнительно хранит gst по паре.

    f1: tier -> wallet -> {conds:set, gmin, gmax} -- дословно как accumulate_pairs.
    f5: tier -> wallet -> {clv:[], cond:[], day:[], gst:[]}.
    """
    f1: dict[str, dict[str, dict]] = {t: {} for t in TIERS}
    f5: dict[str, dict[str, dict]] = {t: {} for t in TIERS}
    total = 0
    for p in pairs:
        total += 1
        tier = p.get("tier")
        if tier not in TIERS:
            continue
        w = (p.get("wallet") or "").strip().lower()
        if not w:
            continue
        d = f1[tier].setdefault(w, {"conds": set(), "gmin": None, "gmax": None})
        d["conds"].add(p.get("cond"))
        gst = p.get("gst")
        if gst is not None:
            if d["gmin"] is None or gst < d["gmin"]:
                d["gmin"] = gst
            if d["gmax"] is None or gst > d["gmax"]:
                d["gmax"] = gst
        clv = p.get("clv")
        if clv is None:
            continue
        r = f5[tier].setdefault(w, {"clv": [], "cond": [], "day": [], "gst": []})
        r["clv"].append(float(clv))
        r["cond"].append(p.get("cond"))
        r["day"].append(day_from_slug(p.get("slug") or "") or (p.get("cond") or "?"))
        r["gst"].append(gst)
    return f1, f5, total


# ------------------------------- расщепление -------------------------------
def split_lists_by_gst(clv, cond, day, gst, start_ts: float, mid_ts: float, end_ts: float):
    """Разбить пары на половины по gst: первая gst < mid, вторая gst >= mid.

    Возвращает (h1, h2, n_placed, n_unplaced), h = {"clv", "cond", "day"}.
    Пары без gst или вне [start_ts, end_ts) не попадают ни в одну половину.
    """
    h1 = {"clv": [], "cond": [], "day": []}
    h2 = {"clv": [], "cond": [], "day": []}
    n_placed = 0
    n_unplaced = 0
    for c, cd, dd, g in zip(clv, cond, day, gst):
        if g is None or not (start_ts <= g < end_ts):
            n_unplaced += 1
            continue
        if g < mid_ts:
            h1["clv"].append(c)
            h1["cond"].append(cd)
            h1["day"].append(dd)
        else:
            h2["clv"].append(c)
            h2["cond"].append(cd)
            h2["day"].append(dd)
        n_placed += 1
    return h1, h2, n_placed, n_unplaced


def half_rows_for_wallet(w: str, entry: dict, start_ts: float, mid_ts: float, end_ts: float) -> dict:
    """Половины кошелька: n, средний CLV и t в каждой (screen_one)."""
    h1, h2, n_placed, n_unplaced = split_lists_by_gst(
        entry["clv"], entry["cond"], entry["day"], entry["gst"], start_ts, mid_ts, end_ts)
    r1 = screen_one(h1["clv"], h1["cond"], h1["day"])
    r2 = screen_one(h2["clv"], h2["cond"], h2["day"])
    return {
        "wallet": w,
        "n_placed": n_placed,
        "n_unplaced": n_unplaced,
        "n1": r1.n_matches,
        "mean1": r1.mean_clv,
        "t1": r1.t,
        "n2": r2.n_matches,
        "mean2": r2.mean_clv,
        "t2": r2.t,
    }


def split_sample(rows: list[dict]) -> list[dict]:
    """Кандидаты с n >= MIN_MATCHES_HALF в ОБЕИХ половинах (выборка корреляции)."""
    return [r for r in rows if r["n1"] >= MIN_MATCHES_HALF and r["n2"] >= MIN_MATCHES_HALF]


def drop_breakdown(rows: list[dict], sample: list[dict]) -> dict:
    """Разбивка выбывших из-за нехватки матчей по стороне нехватки."""
    dropped = [r for r in rows if r not in sample] if len(sample) < len(rows) else []
    n_low1 = sum(1 for r in dropped if r["n1"] < MIN_MATCHES_HALF and r["n2"] >= MIN_MATCHES_HALF)
    n_low2 = sum(1 for r in dropped if r["n2"] < MIN_MATCHES_HALF and r["n1"] >= MIN_MATCHES_HALF)
    n_low_both = sum(1 for r in dropped if r["n1"] < MIN_MATCHES_HALF and r["n2"] < MIN_MATCHES_HALF)
    return {"dropped": len(dropped), "n1_only": n_low1, "n2_only": n_low2, "both": n_low_both}


# ------------------------------- статистика --------------------------------
def betacf(a: float, b: float, x: float) -> float:
    """Непрерывная дробь для регулярной неполной бета (Numerical Recipes)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _BETACF_FPMIN:
        d = _BETACF_FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _BETACF_MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _BETACF_FPMIN:
            d = _BETACF_FPMIN
        c = 1.0 + aa / c
        if abs(c) < _BETACF_FPMIN:
            c = _BETACF_FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _BETACF_FPMIN:
            d = _BETACF_FPMIN
        c = 1.0 + aa / c
        if abs(c) < _BETACF_FPMIN:
            c = _BETACF_FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _BETACF_EPS:
            break
    return h


def betai(a: float, b: float, x: float) -> float:
    """Регулярная неполная бета I_x(a, b), 0 <= x <= 1.

    Первая область: I_x(a,b) = [x^a (1-x)^b / (a B(a,b))] * CF(a,b,x).
    Вторая: I_x(a,b) = 1 - I_{1-x}(b,a), и префактор I_{1-x}(b,a)
    делится на b (аргументы в CF(b,a,1-x) переставлены).
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lnbt = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
            + a * math.log(x) + b * math.log1p(-x))
    bt = math.exp(lnbt)
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * betacf(a, b, x) / a
    return 1.0 - bt * betacf(b, a, 1.0 - x) / b


def t_two_sided_p(t: float, df: float) -> float:
    """Двусторонний p-value для t-статистики (df степеней свободы)."""
    if df <= 0:
        return math.nan
    t = abs(float(t))
    if math.isinf(t):
        return 0.0
    x = df / (df + t * t)
    return betai(df / 2.0, 0.5, x)


def rankdata(a) -> np.ndarray:
    """Ранги (средние при совпадениях), 1-основанные, как scipy.stats.rankdata."""
    a = np.asarray(a, dtype=float)
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty(len(a), dtype=float)
    sorted_a = a[order]
    i = 0
    n = len(a)
    while i < n:
        j = i
        while j + 1 < n and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        ranks[order[i:j + 1]] = avg
        i = j + 1
    return ranks


def pearson_corr(x, y) -> tuple[float, float]:
    """Пирсон: (r, двусторонний p). Требует n >= 3 и ненулевые дисперсии."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n != len(y) or n < 3:
        return math.nan, math.nan
    mx = float(x.mean())
    my = float(y.mean())
    sx = math.sqrt(float(((x - mx) ** 2).sum()))
    sy = math.sqrt(float(((y - my) ** 2).sum()))
    if sx == 0.0 or sy == 0.0:
        return math.nan, math.nan
    r = float((((x - mx) * (y - my)).sum()) / (sx * sy))
    r = max(-1.0, min(1.0, r))
    if abs(r) >= 1.0 - 1e-12:  # вычислительно ровная корреляция
        return math.copysign(1.0, r), 0.0
    t = r * math.sqrt((n - 2) / (1.0 - r * r))
    return r, t_two_sided_p(t, n - 2)


def spearman_corr(x, y) -> tuple[float, float]:
    """Спирмен: (r_s, двусторонний p). Ранги со средними при совпадениях."""
    rx = rankdata(x)
    ry = rankdata(y)
    return pearson_corr(rx, ry)


def correlate(rows: list[dict]) -> dict:
    """Корреляции Пирсона/Спирмена mean1 vs mean2 по выборке с достаточными n."""
    x = np.asarray([r["mean1"] for r in rows], dtype=float)
    y = np.asarray([r["mean2"] for r in rows], dtype=float)
    rp, pp = pearson_corr(x, y)
    rs, ps = spearman_corr(x, y)
    return {"n": len(rows), "pearson_r": rp, "pearson_p": pp,
            "spearman_r": rs, "spearman_p": ps}


# ------------------------------- правило решения ----------------------------
def tier_split_state(r: float, p: float) -> str:
    """Состояние тира по корреляции Пирсона (предобъявленное правило)."""
    if math.isnan(r) or math.isnan(p):
        return "NULL"
    if p < SPLIT_ALPHA and r >= SPLIT_GO_MIN_R:
        return "SUPPORT"
    if p >= SPLIT_ALPHA:
        return "NULL"
    return "AMBIGUOUS"


def split_verdict(states: dict[str, str]) -> str:
    """Итог правила: GO (оба SUPPORT), NO-GO (оба NULL), иначе UNDECIDABLE."""
    if all(s == "SUPPORT" for s in states.values()):
        return "GO"
    if all(s == "NULL" for s in states.values()):
        return "NO-GO"
    return "UNDECIDABLE"


# ------------------------------- плацебо B -------------------------------
def build_placebo_pool(f5: dict, start_ts: float, end_ts: float) -> dict[str, list]:
    """Пул схемы B: все пары с clv и gst внутри окна, элементы (clv, cond, day, gst)."""
    pool: dict[str, list] = {t: [] for t in TIERS}
    for tier in TIERS:
        items: list = []
        for entry in f5[tier].values():
            for c, cd, dd, g in zip(entry["clv"], entry["cond"], entry["day"], entry["gst"]):
                if g is None or not (start_ts <= g < end_ts):
                    continue
                items.append((c, cd, dd, g))
        pool[tier] = items
    return pool


def placebo_splithalf_corr(pool: list, n_sizes: list[int], start_ts: float, mid_ts: float, end_ts: float) -> dict:
    """Калибровка ОДНОГО тира: корреляция половин на синтетических кошельках схемы B.

    Механизм как в funnel_a_placebo.py: rng = random.Random(seed), block_assign,
    сиды 1..20, все синтетические кошельки пулятся. Возвращает корреляции +
    сколько синтетических кошельков всего и сколько попало в выборку.
    """
    xs: list[float] = []
    ys: list[float] = []
    n_total = 0
    n_included = 0
    for seed in SEEDS:
        rng = random.Random(seed)
        blocks = block_assign(pool, n_sizes, rng)
        for _n, items in blocks:
            clv = [it[0] for it in items]
            cond = [it[1] for it in items]
            day = [it[2] for it in items]
            gst = [it[3] for it in items]
            h1, h2, _placed, _unplaced = split_lists_by_gst(
                clv, cond, day, gst, start_ts, mid_ts, end_ts)
            r1 = screen_one(h1["clv"], h1["cond"], h1["day"])
            r2 = screen_one(h2["clv"], h2["cond"], h2["day"])
            n_total += 1
            if r1.n_matches >= MIN_MATCHES_HALF and r2.n_matches >= MIN_MATCHES_HALF:
                xs.append(r1.mean_clv)
                ys.append(r2.mean_clv)
                n_included += 1
    corr = correlate([{"mean1": x, "mean2": y} for x, y in zip(xs, ys)])
    corr["n_total"] = n_total
    corr["n_included"] = n_included
    return corr


# ------------------------------- отчёт --------------------------------------
def _fmt_corr(c: dict) -> str:
    if c["n"] < 3:
        return "n=%d (мало наблюдений)" % c["n"]
    return ("n=%d, Пирсон r=%.4g p=%.4g | Спирмен r=%.4g p=%.4g" % (
        c["n"], c["pearson_r"], c["pearson_p"], c["spearman_r"], c["spearman_p"]))


def render_md(ctx: dict) -> str:
    L: list[str] = []
    L.append("# Расщепление пополам по времени (split-half среднего CLV)")
    L.append("")
    L.append("Окно: `%s .. %s` (по `gameStartTime`). Половины равной длительности: "
             "середина `%s` UTC, первая половина `gst < mid`, вторая `gst >= mid`."
             % (WIN_START, WIN_END_EXCL, ctx["mid_utc"]))
    L.append("")
    L.append("## Факты прогона")
    L.append("")
    L.append("- дата/время (UTC): `%s`" % ctx["ts_utc"])
    L.append("- команда: `%s`" % ctx["cmdline"])
    L.append("- python: `%s`" % ctx["python"])
    L.append("- numpy: `%s`" % ctx["numpy"])
    L.append("- время счёта: `%.1f c`" % ctx["elapsed"])
    L.append("- сиды плацебо: `%s`" % ", ".join(str(s) for s in ctx["seeds"]))
    L.append("- пар всего / ATP / WTA (сверка с артефактом): `%d / %d / %d`" % (
        ctx["pairs_total"], ctx["pairs_tiers"]["atp"], ctx["pairs_tiers"]["wta"]))
    L.append("- кандидаты фильтра 1: ATP `%d`, WTA `%d`; прошли фильтр 5: ATP `%d`, WTA `%d`" % (
        ctx["cand"]["atp"], ctx["cand"]["wta"], ctx["passers"]["atp"], ctx["passers"]["wta"]))
    L.append("")
    L.append("Процедура: средний CLV и t по каждой половине считаются той же функцией, "
             "что и в основном прогоне (`screen_one`). В выборку корреляции входит "
             "кандидат с `n >= %d` в обеих половинах (`MIN_MATCHES_HALF`, половина порога "
             "фильтра 1)." % MIN_MATCHES_HALF)
    L.append("")
    L.append("## Правило решения (объявлено до прогона)")
    L.append("")
    L.append("- состояние тира по Пирсону: `SUPPORT` если r >= %.2f и p < %.2f; `NULL` если p >= %.2f; иначе `AMBIGUOUS`." % (
        SPLIT_GO_MIN_R, SPLIT_ALPHA, SPLIT_ALPHA))
    L.append("- `GO`: оба тира SUPPORT -> разброс настоящий, идём на этап 2.")
    L.append("- `NO-GO`: оба тира NULL -> ширина есть артефакт ошибки, этап 2 не начинаем.")
    L.append("- иначе `UNDECIDABLE`, ничего не подгоняем.")
    L.append("- Решающая статистика -- корреляция Пирсона; Спирмен -- робастность.")
    L.append("")
    L.append("## 1. Корреляции половин по реальным кандидатам")
    L.append("")
    L.append("| тир | кандидатов | в обеих половинах >= %d | выбыло | выбыло n1 < %d | выбыло n2 < %d | выбыло обе < %d |" % (
        MIN_MATCHES_HALF, MIN_MATCHES_HALF, MIN_MATCHES_HALF, MIN_MATCHES_HALF))
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for t in TIERS:
        s = ctx["real_stats"][t]
        L.append("| %s | %d | %d | %d | %d | %d | %d |" % (
            t.upper(), s["candidates"], s["included"], s["dropped"],
            s["n1_only"], s["n2_only"], s["both"]))
    L.append("")
    L.append("| тир | n | Пирсон r | p | Спирмен r | p |")
    L.append("|---|---:|---:|---:|---:|---:|")
    for t in TIERS:
        c = ctx["real_stats"][t]["corr"]
        L.append("| %s | %d | %.4g | %.4g | %.4g | %.4g |" % (
            t.upper(), c["n"], c["pearson_r"], c["pearson_p"],
            c["spearman_r"], c["spearman_p"]))
    L.append("")
    L.append("## 2. Калибровка: корреляция половин на синтетических кошельках плацебо B")
    L.append("")
    L.append("Ожидание -- около нуля. Сиды 1..20, все синтетические кошельки пулятся.")
    L.append("")
    L.append("| тир | синт. кошельков всего | в выборке (обе половины >= %d) | Пирсон r | p | Спирмен r | p |" % MIN_MATCHES_HALF)
    L.append("|---|---:|---:|---:|---:|---:|---:|")
    for t in TIERS:
        c = ctx["placebo_corr"][t]
        L.append("| %s | %d | %d | %.4g | %.4g | %.4g | %.4g |" % (
            t.upper(), c["n_total"], c["n_included"], c["pearson_r"],
            c["pearson_p"], c["spearman_r"], c["spearman_p"]))
    L.append("")
    L.append("## 4. Прошедшие фильтр 5: средний CLV и t по половинам")
    L.append("")
    L.append("Адреса только в `probes/deepseek/splithalf_passers.json`; в таблице -- индексы.")
    L.append("")
    for t in TIERS:
        L.append("### %s (прошло %d)" % (t.upper(), ctx["passers"][t]))
        L.append("")
        L.append("| idx | n1 | mean1 | t1 | n2 | mean2 | t2 |")
        L.append("|---|---:|---:|---:|---:|---:|---:|")
        for r in ctx["passer_rows"][t]:
            L.append("| %s | %d | %.4g | %.4g | %d | %.4g | %.4g |" % (
                r["idx"], r["n1"], r["mean1"], r["t1"], r["n2"], r["mean2"], r["t2"]))
        L.append("")
    L.append("## 5. Средний CLV > 0 во второй половине среди прошедших")
    L.append("")
    L.append("| тир | прошедших | положительных во второй половине |")
    L.append("|---|---:|---:|")
    for t in TIERS:
        L.append("| %s | %d | %d |" % (t.upper(), ctx["passers"][t], ctx["pos_second"][t]))
    L.append("")
    L.append("## Вердикт по предобъявленному правилу")
    L.append("")
    L.append("| тир | Пирсон r | p | состояние |")
    L.append("|---|---:|---:|---|")
    for t in TIERS:
        c = ctx["real_stats"][t]["corr"]
        L.append("| %s | %.4g | %.4g | %s |" % (
            t.upper(), c["pearson_r"], c["pearson_p"], ctx["states"][t]))
    L.append("")
    L.append("Итог: **%s**" % ctx["verdict"])
    L.append("")
    return "\n".join(L)


# ------------------------------- главный прогон -----------------------------
def run() -> int:
    start = time.time()
    ts_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    cmdline = " ".join(sys.argv)

    print("funnel_a_splithalf.py -- расщепление пополам по времени")
    print("ts_utc        : %s" % ts_utc)
    print("command       : %s" % cmdline)
    print("python        : %s" % sys.version.split()[0])
    print("numpy         : %s" % np.__version__)

    start_ts, end_ts, mid_ts = window_bounds()
    print("\n-- окно --")
    print("start %s UTC (%.0f) | mid %s UTC (%.0f) | end %s UTC (%.0f)" % (
        WIN_START, start_ts,
        datetime.fromtimestamp(mid_ts, tz=timezone.utc).strftime("%Y-%m-%d"), mid_ts,
        WIN_END_EXCL, end_ts))
    print("длительность половины: %.1f суток" % ((mid_ts - start_ts) / 86400.0))

    # 1) один проход артефакта
    print("\n-- чтение артефакта (один проход, с gst по паре) --")
    f1, f5, pairs_total = accumulate_pairs_with_gst(iter_artifact_pairs(ARTIFACT))
    print("пар всего                          : %d" % pairs_total)
    print("пар ATP / WTA (строк с clv != None) : %d / %d" % (
        sum(len(v["clv"]) for v in f5["atp"].values()),
        sum(len(v["clv"]) for v in f5["wta"].values())))

    # 2) фильтр 1 и фильтр 5 тем же кодом (сверка с основным прогоном)
    f1p, f1d = filter1_pass(f1)
    f5p, f5res = filter5_screen(f5, f1p)
    print("-- сверка с основным прогоном --")
    for tier in TIERS:
        print("%s: кандидатов фильтра 1 %d, прошли фильтр 5 %d" % (
            tier.upper(), len(f1p[tier]), len(f5p[tier])))

    # 3) половины по реальным кандидатам
    n_by_tier: dict[str, list[int]] = {}
    rows_by_tier: dict[str, list[dict]] = {}
    for tier in TIERS:
        cand = sorted(f1p[tier])
        n_by_tier[tier] = [f1d[tier][w]["n_matches"] for w in cand]
        rows_by_tier[tier] = [
            half_rows_for_wallet(w, f5[tier].get(w) or {"clv": [], "cond": [], "day": [], "gst": []},
                                 start_ts, mid_ts, end_ts) for w in cand]

    print("\n-- корреляции половин по реальным кандидатам --")
    real_stats = {}
    for tier in TIERS:
        rows = rows_by_tier[tier]
        sample = split_sample(rows)
        drop = drop_breakdown(rows, sample)
        corr = correlate(sample)
        real_stats[tier] = {
            "candidates": len(rows),
            "included": len(sample),
            "dropped": drop["dropped"],
            "n1_only": drop["n1_only"],
            "n2_only": drop["n2_only"],
            "both": drop["both"],
            "corr": corr,
        }
        s = real_stats[tier]
        print("%s: кандидатов %d, в обеих половинах >= %d: %d, выбыло %d "
              "(n1<50: %d, n2<50: %d, обе<50: %d)" % (
            tier.upper(), s["candidates"], MIN_MATCHES_HALF, s["included"],
            s["dropped"], s["n1_only"], s["n2_only"], s["both"]))
        print("%s: корреляция %s" % (tier.upper(), _fmt_corr(corr)))

    # 4) плацебо B: калибровка
    print("\n-- калибровка: корреляция половин на плацебо схемы B (сиды 1..20) --")
    pool_by_tier = build_placebo_pool(f5, start_ts, end_ts)
    print("пул пар ATP / WTA: %d / %d" % (len(pool_by_tier["atp"]), len(pool_by_tier["wta"])))
    placebo_corr = {}
    for tier in TIERS:
        c = placebo_splithalf_corr(pool_by_tier[tier], n_by_tier[tier], start_ts, mid_ts, end_ts)
        placebo_corr[tier] = c
        print("%s: синт. кошельков %d, в выборке %d, корреляция %s" % (
            tier.upper(), c["n_total"], c["n_included"], _fmt_corr(c)))

    # 5) прошедшие: таблица половин и счёт положительных во второй
    passer_rows: dict[str, list[dict]] = {}
    pos_second: dict[str, int] = {}
    for tier in TIERS:
        row_by_wallet = {r["wallet"]: r for r in rows_by_tier[tier]}
        passers = [w for w in sorted(f5p[tier]) if w in row_by_wallet]
        rows = []
        for i, w in enumerate(passers, start=1):
            r = row_by_wallet[w]
            rows.append({"idx": "%s%02d" % (tier[0], i),
                         "wallet": w,
                         "n1": r["n1"], "mean1": r["mean1"], "t1": r["t1"],
                         "n2": r["n2"], "mean2": r["mean2"], "t2": r["t2"]})
        passer_rows[tier] = rows
        pos_second[tier] = sum(1 for r in rows if r["mean2"] > 0.0)
        print("\n%s: прошедшие, положительных во второй половине: %d из %d" % (
            tier.upper(), pos_second[tier], len(rows)))
        for r in rows:
            print("  %s: n1 %d, mean1 %.4g, t1 %.4g | n2 %d, mean2 %.4g, t2 %.4g" % (
                r["idx"], r["n1"], r["mean1"], r["t1"], r["n2"], r["mean2"], r["t2"]))

    # 6) вердикт
    states = {t: tier_split_state(real_stats[t]["corr"]["pearson_r"],
                                   real_stats[t]["corr"]["pearson_p"]) for t in TIERS}
    verdict = split_verdict(states)
    print("\n-- вердикт (правило объявлено до прогона) --")
    for tier in TIERS:
        c = real_stats[tier]["corr"]
        print("%s: r=%.4g, p=%.4g -> %s" % (
            tier.upper(), c["pearson_r"], c["pearson_p"], states[tier]))
    print("итог: %s" % verdict)

    # 7) отчёты на диск
    ctx = {
        "ts_utc": ts_utc,
        "cmdline": cmdline,
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "elapsed": time.time() - start,
        "seeds": SEEDS,
        "mid_utc": datetime.fromtimestamp(mid_ts, tz=timezone.utc).strftime("%Y-%m-%d"),
        "pairs_total": pairs_total,
        "pairs_tiers": {t: sum(len(v["clv"]) for v in f5[t].values()) for t in TIERS},
        "cand": {t: len(f1p[t]) for t in TIERS},
        "passers": {t: len(f5p[t]) for t in TIERS},
        "real_stats": real_stats,
        "placebo_corr": placebo_corr,
        "passer_rows": {t: [{k: v for k, v in r.items() if k != "wallet"} for r in passer_rows[t]] for t in TIERS},
        "pos_second": pos_second,
        "states": states,
        "verdict": verdict,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(render_md(ctx) + "\n")
    print("\nзаписан отчёт: %s" % OUT_MD)

    payload = {
        "window": {"start": WIN_START, "end_excl": WIN_END_EXCL,
                   "mid": datetime.fromtimestamp(mid_ts, tz=timezone.utc).strftime("%Y-%m-%d")},
        "note": "Адреса прошедших фильтр 5 и их половины. Только здесь, не в чате.",
        "min_matches_half": MIN_MATCHES_HALF,
        "tiers": {
            t: [{"wallet": r["wallet"], "n1": r["n1"], "mean1": r["mean1"], "t1": r["t1"],
                 "n2": r["n2"], "mean2": r["mean2"], "t2": r["t2"]} for r in passer_rows[t]]
            for t in TIERS
        },
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
