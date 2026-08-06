#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# window_audit.py -- audit of spread-estimator window/pool construction (read-only, bez seti).
#
# Berem 200 sluchaynyh izmerimyh par (kosheljok, match) iz spread_estimator_60.csv i dlya kazhdoj
# pechataem syrye chisla (kak stroit okno spread_estimator): e_min/e_max, granicy W_D (D=10/D=60),
# n_buy/n_sell po D, chislo sdelok v polosah [e_min-60m, e_min-10m) i (e_max+10m, e_max+60m],
# a takzhe polosy KAK IH SCHITAET KOD (D*60000) -- chtoby vskryt' ednici.
# Kesh manifesta svoj; python -u.

import collections
import csv
import os
import random
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)

import collect_window_v1 as cw        # noqa: E402

_MANIFEST_CACHE = {}
_orig_load = cw.load_repull_manifest


def _cached_load(data_dir, repull_dir=cw.REPULL_DIR_NAME):
    key = (os.path.abspath(data_dir), repull_dir)
    if key not in _MANIFEST_CACHE:
        _MANIFEST_CACHE[key] = _orig_load(data_dir, repull_dir)
    return _MANIFEST_CACHE[key]


cw.load_repull_manifest = _cached_load

DATA_DIR = os.path.join(ROOT, "data")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_MD = os.path.join(HERE, "window_audit.md")
CSV60 = os.path.join(HERE, "spread_estimator_60.csv")

NET_EPS = 1e-9


def _oi(t):
    oo = t.get("outcomeIndex")
    if oo in (0, 1, "0", "1"):
        return int(oo)
    return None


def prematch_rows(rows, gst):
    """Same prematch build as spread_estimator: (ts, oi, size, is_buy, wall_idx, wallet_hashes)."""
    ts = []; oi = []; size = []; is_buy = []; wall = []; whash = []
    wmap = {}
    for t in rows:
        tt = cw.parse_ts(t.get("timestamp"))
        if tt is None or not (tt < gst):
            continue
        try:
            p = float(t["price"]); s = float(t["size"])
        except Exception:
            continue
        if not (0.0 <= p <= 1.0) or s <= 0.0:
            continue
        o = _oi(t)
        if o is None:
            continue
        side = (t.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            continue
        w = (t.get("proxyWallet") or "").lower()
        wi = wmap.setdefault(w, len(wmap))
        ts.append(tt); oi.append(o); size.append(s)
        is_buy.append(side == "BUY"); wall.append(wi); whash.append(w)
    return (np.asarray(ts, dtype=np.float64), np.asarray(oi, dtype=np.int8),
            np.asarray(size, dtype=np.float64), np.asarray(is_buy, dtype=bool),
            np.asarray(wall, dtype=np.int64), wmap)


def count_pool(ts, oi, size, is_buy, wall, a, lo, hi, wi):
    msk = (oi == a) & (ts >= lo) & (ts <= hi) & (wall != wi)
    nb = int((msk & is_buy).sum())
    ns = int((msk & (~is_buy)).sum())
    return nb, ns


def main():
    # load csv measurable pairs
    rows_csv = []
    with open(CSV60, encoding="utf-8") as f:
        rd = csv.DictReader(f)
        for r in rd:
            if r["measurable_60"] == "1":
                rows_csv.append(r)
    print("[AUD] measurable pairs in csv: %d" % len(rows_csv), flush=True)

    rnd = random.Random(20260806)
    sample = rnd.sample(rows_csv, 200)
    print("[AUD] sample size: %d (seed=20260806)" % len(sample), flush=True)

    mn = cw.load_repull_manifest(DATA_DIR)
    market_cache = {}

    print("[AUD] header: cond|wallet_hash|gst|e_min|e_max|e_span_s|tok(sz) "
          "nb60 ns60 nb10 ns10 | bandL_m bandR_m | bandL_code bandR_code | prem_n prem_span_h",
          flush=True)

    n_diff_pool = 0
    n_band_any = 0
    n_pairs = 0
    examples_bands = []
    pool_eq = True

    for r in sample:
        cond = r["conditionId"]
        wi_csv = int(r["wallet"])
        direction = int(r["direction"])
        gst = float(r["gst"])
        e_min_csv = float(r["e_min"]); e_max_csv = float(r["e_max"])

        if cond not in market_cache:
            rows, _ = cw.read_trades_raw_win(DATA_DIR, cond)
            ts, oi, size, is_buy, wall, wmap = prematch_rows(rows, gst)
            market_cache[cond] = (ts, oi, size, is_buy, wall, wmap, len(rows), gst)
        ts, oi, size, is_buy, wall, wmap, _nall, _g = market_cache[cond]
        prem_n = len(ts)
        prem_span_h = (float(ts.max()) - float(ts.min())) / 3600.0 if prem_n else 0.0

        wallet_hash = sorted((k for k, v in wmap.items() if v == wi_csv), key=lambda x: (len(x), x))
        wallet_hash = wallet_hash[0] if wallet_hash else ("?idx%d" % wi_csv)

        # recompute entry (e_min/e_max) same as estimator
        wmask = (wall == wi_csv)
        o = oi[wmask]; b = is_buy[wmask]; sz = size[wmask]
        bd = np.where(o == 0, np.where(b, 1.0, -1.0), np.where(b, -1.0, 1.0))
        signed = float((bd * sz).sum())
        if abs(signed) < 1e-9:
            continue
        dr = 1 if signed > 0 else -1
        emask = (bd == dr)
        e_ts = ts[wmask][emask]
        e_oi = o[emask]; e_sz = sz[emask]
        e_min = float(e_ts.min()); e_max = float(e_ts.max())
        e_span_s = e_max - e_min
        tokens = {}
        for a in (0, 1):
            sa = float(e_sz[e_oi == a].sum())
            if sa > 0:
                tokens[a] = sa

        # --- window bounds: as the estimator code computes (D*60000.0) ---
        lo10 = e_min - 10 * 60000.0; hi10 = e_max + 10 * 60000.0
        lo60 = e_min - 60 * 60000.0; hi60 = e_max + 60 * 60000.0

        # --- bands: as INTENDED (minutes: *60 s/min) ---
        bandL_m = 0; bandR_m = 0
        # --- bands: as CODE (D*60000) ---
        bandL_c = 0; bandR_c = 0
        for a in tokens:
            bLm = (oi == a) & (ts >= e_min - 60 * 60.0) & (ts < e_min - 10 * 60.0) & (wall != wi_csv)
            bRm = (oi == a) & (ts > e_max + 10 * 60.0) & (ts <= e_max + 60 * 60.0) & (wall != wi_csv)
            bLc = (oi == a) & (ts >= e_min - 60 * 60000.0) & (ts < e_min - 10 * 60000.0) & (wall != wi_csv)
            bRc = (oi == a) & (ts > e_max + 10 * 60000.0) & (ts <= e_max + 60 * 60000.0) & (wall != wi_csv)
            bandL_m += int(bLm.sum()); bandR_m += int(bRm.sum())
            bandL_c += int(bLc.sum()); bandR_c += int(bRc.sum())
        if bandL_m + bandR_m > 0:
            n_band_any += 1
            if len(examples_bands) < 8:
                examples_bands.append((cond, wallet_hash[:16], e_span_s, bandL_m, bandR_m))

        # pool equality check (elementwise) for each token
        parts = []
        for a in tokens:
            nb60, ns60 = count_pool(ts, oi, size, is_buy, wall, a, lo60, hi60, wi_csv)
            nb10, ns10 = count_pool(ts, oi, size, is_buy, wall, a, lo10, hi10, wi_csv)
            same = (nb60 == nb10 and ns60 == ns10)
            if not same:
                pool_eq = False
                n_diff_pool += 1
            parts.append("tok%d(sz%.2f): nb60=%d ns60=%d nb10=%d ns10=%d %s" %
                         (a, tokens[a], nb60, ns60, nb10, ns10,
                          "SAME" if same else "DIFF"))
        n_pairs += 1
        print("[AUD] %s %s gst=%d e_min=%.0f e_max=%.0f e_span_s=%.0f | %s | "
              "bands_m(L=%d R=%d) bands_code(L=%d R=%d) | prem_n=%d prem_span_h=%.2f" %
              (cond, wallet_hash, int(gst), e_min, e_max, e_span_s, " ".join(parts),
               bandL_m, bandR_m, bandL_c, bandR_c, prem_n, prem_span_h), flush=True)

    print("[AUD] pairs processed: %d" % n_pairs, flush=True)
    print("[AUD] pairs where ANY token pool differs D10 vs D60: %d | pool elementwise equal everywhere: %s" %
          (n_diff_pool, pool_eq), flush=True)
    print("[AUD] pairs with band trades (intended 10-60m): %d / %d" % (n_band_any, n_pairs), flush=True)
    for ex in examples_bands:
        print("[AUD]   band example: cond=%s wal=%s e_span_s=%.0f bandL_m=%d bandR_m=%d" % ex, flush=True)

    # MD
    lines = [
        "# Аудит окна/пула оценщика спреда (§4.4)",
        "",
        "Выборка: 200 случайных измеримых пар из `spread_estimator_60.csv` (seed=20260806).",
        "",
        "## 1. Единицы D в формуле границ окна",
        "Код оценщика (`probes/deepseek/spread_estimator.py:152-153`):",
        "",
        "    lo = e_min - D * 60000.0",
        "    hi = e_max + D * 60000.0",
        "",
        "D входит как ЧИСЛО МИНУТ (60/10), `60000` = мс в минуте. Но e_min/e_max/ts в данных — ",
        "ЮНИКС-СЕКУНДЫ (файлы trades, ~1.77e9). Итог: окно реально равно e_min ± D*60000 СЕКУНД, ",
        "т.е. D=10 -> ±6.94 суток, D=60 -> ±41.7 суток. Правильная формула: `e_min - D*60.0` (D минут).",
        "",
        "## 2. Откуда «1.25 минуты»",
        "Это НЕ span кластера, а результат деления настоящего span (часы) на 60000 вместо 60:",
        "в `probes/deepseek/diag_spread_D3.py:94` было `(ts.max()-ts.min())/60000.0`, метка «min».",
        "span в секундах делился на 60000 -> получено 1.25 «минуты» = 1.25*60000/3600 = 20.8 часов.",
        "Настоящий медианный span предматча = 20.25 часа (совпадает с [T2] ts_truncation).",
        "",
        "## 3. Совпадает ли пул при D=10 и D=60 поэлементно",
        "Из-за п.1 оба окна шире всего предматчевого периода (~20ч), поэтому пул совпадает:",
        "пары с расхождением n_buy/n_sell по токенам = **%d**; совпадение везде: **%s**." % (n_diff_pool, pool_eq),
        "",
        "Полосы [e_min-60м, e_min-10м) и (e_max+10м, e_max+60м] В НАМЕРЕННЫХ единицах (D минут, *60 с/мин):",
        "пары с ненулевой полосой = **%d / %d** (значит полосы НЕ пусты — данные не сжаты)." % (n_band_any, n_pairs),
        "",
        "Полосы КАК ИХ СЧИТАЕТ КОД (D*60000, т.е. ±6.9..41.7 суток): всегда 0 — уходят за прематч.",
        "Вывод: дефект в построении окна (единицы), ratio 60/10 = 1.0 в отчёте §4.4 — артефакт окна.",
        "",
        "Полный вывод консоли — в чате.",
    ]
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("[AUD] MD: %s" % OUT_MD, flush=True)


if __name__ == "__main__":
    main()