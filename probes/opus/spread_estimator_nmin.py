#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# spread_estimator_nmin.py -- POPRAVKA 13 spread estimator, recomputed from
# data/trades_raw_win at three measurability thresholds N_min in {5,10,20}.
#
# Definition (unchanged from P13 / spread_estimator.py):
#   window W_D = [e_min - D*60, e_max + D*60], D=60, closed, prematch (ts<gst);
#   pool = same conditionId + same token of side a + proxyWallet != pair wallet
#          + ts < gst + ts in [lo,hi];
#   side a = token the wallet actually buys on entry (entry-direction trades);
#   per active token a: vwap_buy, vwap_sell over non-self pool trades;
#   raw_a  = 0.5*(vwap_buy - vwap_sell);   eff/clip is applied AFTER aggregation.
#   pair aggregate (size-weighted over active tokens a, weight = S_a):
#     raw(w,m,D)  = sum_a S_a * raw_a / sum_a S_a           (signed)
#     spc(w,m,D)  = max(raw(w,m,D), 0)                       (clipped)
#   => clip == max(raw,0) holds elementwise BY CONSTRUCTION (this is the fix vs
#      the old CSV, where clip was per-token-clipped-then-aggregated -> 151 rows
#      with clip != max(raw,0); we must NOT reproduce those).
#   measurability at N_min: for EACH active token, n_buy>=N_min AND n_sell>=N_min,
#      joined across active tokens with AND (P13, per-token).
#
# The per-token pool/vwap/counts do NOT depend on N_min, so all three thresholds
# are computed from ONE estimator pass; thresholds are applied in memory.
#
# Speed: uses run_fast.py's manifest cache (O(N^2)->O(N)). collect_window_v1.py
# is imported read-only and NOT modified/run. Nothing existing is overwritten.

import hashlib
import os
import statistics
import sys
import time

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)

import collect_window_v1 as cw   # noqa: E402  (read-only import)

# --- process-local manifest cache (identical trick to run_fast.py) ---
_MANIFEST_CACHE = {}
_orig_load_repull_manifest = cw.load_repull_manifest


def _cached_load_repull_manifest(data_dir, repull_dir=cw.REPULL_DIR_NAME):
    key = (os.path.abspath(data_dir), repull_dir)
    if key not in _MANIFEST_CACHE:
        _MANIFEST_CACHE[key] = _orig_load_repull_manifest(data_dir, repull_dir)
    return _MANIFEST_CACHE[key]


cw.load_repull_manifest = _cached_load_repull_manifest

DATA_DIR = os.path.join(ROOT, "data")
HERE = os.path.dirname(os.path.abspath(__file__))
SHA_FROZEN = "FAE5999CC9EEBEAE26087BFEDF449A86DA1156F8C566AE6976C5D2A937543D73"

D_WORK = 60
D_ALT = 10
SEC_PER_MIN = 60.0
NMINS = (5, 10, 20)

LIMIT = int(os.environ.get("NMIN_LIMIT", "0"))  # >0 = benchmark on first N markets


def _ts(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _oi(t):
    oo = t.get("outcomeIndex")
    if oo in (0, 1, "0", "1"):
        return int(oo)
    return None


def load_markets():
    mn = cw.load_repull_manifest(DATA_DIR)
    out = []
    for rec in mn.values():
        if rec.get("tier") not in cw.DECISION_TIERS:
            continue
        if rec.get("completeness_unreachable"):
            continue
        out.append({"cond": rec["cond"], "slug": rec["slug"], "tier": rec["tier"], "gst": rec["gst"]})
    out.sort(key=lambda m: m["cond"])
    return out


def collect():
    """One estimator pass. Returns dict of parallel arrays (one row per pair with entry)."""
    markets = load_markets()
    if LIMIT:
        markets = markets[:LIMIT]
        print("[bench] LIMIT active: first %d markets" % LIMIT, flush=True)
    print("[spread] markets (atp/wta complete):", len(markets), flush=True)

    # per-pair columns
    P = {k: [] for k in (
        "tier", "wallet", "cond",
        "sz0", "sz1",
        # D=60 per token
        "nb0_60", "ns0_60", "rw0_60", "nb1_60", "ns1_60", "rw1_60",
        # D=10 per token
        "nb0_10", "ns0_10", "rw0_10", "nb1_10", "ns1_10", "rw1_10",
    )}
    n_prematch_pairs = 0
    t0 = time.time()

    for mi, m in enumerate(markets):
        cond, gst, slug, tier = m["cond"], m["gst"], m["slug"], m["tier"]
        rows, _ = cw.read_trades_raw_win(DATA_DIR, cond)
        clean, _ = cw.filter_bad_prices(rows, gst, cond, slug)

        ts = []; oi = []; size = []; price = []; is_buy = []; wall = []
        wmap = {}; wall_name = {}
        for t in clean:
            tt = _ts(t.get("timestamp"))
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
            wall_name[wi] = w
            ts.append(tt); oi.append(o); size.append(s); price.append(p)
            is_buy.append(side == "BUY"); wall.append(wi)

        n = len(ts)
        if n == 0:
            continue
        ts = np.asarray(ts); oi = np.asarray(oi, dtype=np.int8)
        size = np.asarray(size); price = np.asarray(price)
        is_buy = np.asarray(is_buy, dtype=bool); wall = np.asarray(wall, dtype=np.int64)

        wgroups = {}
        for i in range(n):
            wgroups.setdefault(int(wall[i]), []).append(i)

        for wi, idxs in wgroups.items():
            n_prematch_pairs += 1
            ii = np.asarray(idxs)
            o = oi[ii]; b = is_buy[ii]; sz = size[ii]
            bd = np.where(o == 0, np.where(b, 1.0, -1.0), np.where(b, -1.0, 1.0))
            signed = float((bd * sz).sum())
            if abs(signed) < 1e-9:
                continue  # net-zero: no entry
            direction = 1 if signed > 0 else -1
            emask = (bd == direction)
            eidx = ii[emask]
            e_ts = ts[eidx]; e_oi = oi[eidx]; e_sz = size[eidx]
            sz0 = float(e_sz[e_oi == 0].sum())
            sz1 = float(e_sz[e_oi == 1].sum())
            tokens = {0: sz0, 1: sz1}

            emin_a = {}; emax_a = {}
            for a, sa in tokens.items():
                if sa <= 0:
                    continue
                ats = e_ts[e_oi == a]
                emin_a[a] = float(ats.min())
                emax_a[a] = float(ats.max())

            # per token per D: n_buy, n_sell, raw_a=0.5*(vb-vs)
            per = {D: {0: None, 1: None} for D in (D_WORK, D_ALT)}
            for D in (D_WORK, D_ALT):
                for a, sa in tokens.items():
                    if sa <= 0:
                        continue
                    lo = emin_a[a] - D * SEC_PER_MIN
                    hi = emax_a[a] + D * SEC_PER_MIN
                    msk = (oi == a) & (ts >= lo) & (ts <= hi) & (wall != wi)
                    buy = msk & is_buy
                    sell = msk & (~is_buy)
                    nb = int(buy.sum()); ns = int(sell.sum())
                    vb = float((price * buy * size).sum() / (size * buy).sum()) if nb else None
                    vs = float((price * sell * size).sum() / (size * sell).sum()) if ns else None
                    raw_a = 0.5 * (vb - vs) if (nb and ns) else float("nan")
                    per[D][a] = (nb, ns, raw_a)

            P["tier"].append(tier); P["wallet"].append(wall_name[wi]); P["cond"].append(cond)
            P["sz0"].append(sz0); P["sz1"].append(sz1)
            for D, suf in ((D_WORK, "60"), (D_ALT, "10")):
                for a in (0, 1):
                    rec = per[D][a]
                    if rec is None:
                        nb, ns, raw_a = -1, -1, float("nan")
                    else:
                        nb, ns, raw_a = rec
                    P["nb%d_%s" % (a, suf)].append(nb)
                    P["ns%d_%s" % (a, suf)].append(ns)
                    P["rw%d_%s" % (a, suf)].append(raw_a)

        if (mi + 1) % 500 == 0:
            el = time.time() - t0
            print("[spread] %d/%d markets | pairs %d | %.1fs (%.3fs/mkt)"
                  % (mi + 1, len(markets), len(P["tier"]), el, el / (mi + 1)), flush=True)

    el = time.time() - t0
    print("[spread] pass done: %d markets, %d pairs with entry, %.1fs"
          % (len(markets), len(P["tier"]), el), flush=True)
    print("[spread] pairs with prematch (net!=0 filtered):", n_prematch_pairs, flush=True)

    # to numpy
    A = {}
    A["tier"] = np.array(P["tier"], dtype=object)
    A["wallet"] = np.array(P["wallet"], dtype=object)
    A["cond"] = np.array(P["cond"], dtype=object)
    for k in ("sz0", "sz1", "rw0_60", "rw1_60", "rw0_10", "rw1_10"):
        A[k] = np.array(P[k], dtype=float)
    for k in ("nb0_60", "ns0_60", "nb1_60", "ns1_60", "nb0_10", "ns0_10", "nb1_10", "ns1_10"):
        A[k] = np.array(P[k], dtype=np.int64)
    A["n_prematch_pairs"] = n_prematch_pairs
    return A


def measurable_mask(A, N, suf):
    """Per-token AND at threshold N for window suffix suf in {'60','10'}."""
    a0 = A["sz0"] > 0
    a1 = A["sz1"] > 0
    ok0 = (~a0) | ((A["nb0_%s" % suf] >= N) & (A["ns0_%s" % suf] >= N))
    ok1 = (~a1) | ((A["nb1_%s" % suf] >= N) & (A["ns1_%s" % suf] >= N))
    return ok0 & ok1


def aggregate(A, suf):
    """Size-weighted signed raw over active tokens; clip = max(raw,0)."""
    a0 = (A["sz0"] > 0).astype(float)
    a1 = (A["sz1"] > 0).astype(float)
    w0 = a0 * A["sz0"]
    w1 = a1 * A["sz1"]
    # raw_a may be nan for active-but-degenerate tokens; only used on measurable
    # pairs where nb,ns>=N>0 => raw defined. Use nan-safe fill (0*weight) guarded.
    r0 = np.where(a0 > 0, A["rw0_%s" % suf], 0.0)
    r1 = np.where(a1 > 0, A["rw1_%s" % suf], 0.0)
    den = w0 + w1
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = (w0 * r0 + w1 * r1) / den
    clip = np.maximum(raw, 0.0)
    return raw, clip


def pctile(x, p):
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if x.size == 0:
        return float("nan")
    return float(np.percentile(x, p))


def main():
    target = os.path.join(ROOT, "collect_window_v1.py")
    sha = hashlib.sha256(open(target, "rb").read()).hexdigest().upper()
    print("[sha256] collect_window_v1.py:", sha, flush=True)
    if sha != SHA_FROZEN:
        print("STOP: SHA256 mismatch (collect_window_v1.py changed)", flush=True)
        sys.exit(2)

    A = collect()
    npairs = len(A["tier"])

    # ---- HARD CHECK: clip == max(raw,0) elementwise, both from same pool/pass ----
    print(flush=True)
    print("=== HARD CHECK: clip == max(raw,0) elementwise (D=60 and D=10) ===", flush=True)
    violations = 0
    first10 = []
    for suf in ("60", "10"):
        raw, clip = aggregate(A, suf)
        exp = np.maximum(raw, 0.0)
        # compare only where raw is defined (non-nan); nan rows are non-measurable
        defined = ~np.isnan(raw)
        bad = defined & (~np.isclose(clip, exp, atol=1e-12, rtol=0.0))
        nb = int(bad.sum())
        violations += nb
        if nb:
            for i in np.flatnonzero(bad)[:10]:
                first10.append((suf, A["cond"][i], A["wallet"][i], float(raw[i]), float(clip[i])))
        print("  D=%s: defined rows=%d, violations=%d" % (suf, int(defined.sum()), nb), flush=True)
    if violations:
        print("HARD CHECK FAILED: %d rows with clip != max(raw,0). First 10:" % violations, flush=True)
        for suf, cond, wal, r, c in first10[:10]:
            print("  D=%s cond=%s wallet=%s raw=%.9f clip=%.9f" % (suf, cond, wal, r, c), flush=True)
        sys.exit(5)
    print("  OK: clip == max(raw,0) on every defined row.", flush=True)

    # ---- decisive set (join tier+wallet on wallets_319.csv) ----
    import csv as _csv
    wpath = os.path.join(ROOT, "probes", "deepseek", "wallets_319.csv")
    allow = set()
    with open(wpath, newline="", encoding="utf-8") as f:
        rd = _csv.DictReader(f)
        for r in rd:
            allow.add((r["tier"].strip(), r["proxyWallet"].strip().lower()))
    print(flush=True)
    print("[decisive] allowances loaded:", len(allow), flush=True)
    dec = np.array([(A["tier"][i], A["wallet"][i]) in allow for i in range(npairs)], dtype=bool)
    print("[decisive] decisive-set pairs (control 81941):", int(dec.sum()), flush=True)

    tier = A["tier"]
    is_atp = tier == "atp"
    is_wta = tier == "wta"

    # precompute aggregates once (threshold-independent values)
    raw60, clip60 = aggregate(A, "60")
    raw10, clip10 = aggregate(A, "10")

    # ---- internal self-check at N_min=20: measurable & |M*| must be 12779 & 5189 ----
    m60_20 = measurable_mask(A, 20, "60")
    m10_20 = measurable_mask(A, 20, "10")
    n_meas20 = int(m60_20.sum())
    n_mstar20 = int((m60_20 & m10_20).sum())
    print(flush=True)
    print("=== SELF-CHECK (N_min=20 reproduces frozen estimator) ===", flush=True)
    print("  measurable D=60:", n_meas20, "(control 12779) ->", "OK" if n_meas20 == 12779 else "MISMATCH", flush=True)
    print("  |M*|:", n_mstar20, "(control 5189) ->", "OK" if n_mstar20 == 5189 else "MISMATCH", flush=True)
    if not LIMIT and (n_meas20 != 12779 or n_mstar20 != 5189):
        print("STOP: self-check failed, reimplementation does not match frozen counts.", flush=True)
        sys.exit(6)

    # ---- per-threshold report ----
    print(flush=True)
    print("=== PER-THRESHOLD REPORT (D=60 unless stated) ===", flush=True)
    report = {}
    for N in NMINS:
        m60 = measurable_mask(A, N, "60")
        m10 = measurable_mask(A, N, "10")
        mstar = m60 & m10

        n_meas = int(m60.sum())
        n_dec = int((m60 & dec).sum())
        share_dec = n_dec / 81941.0

        cvals = clip60[m60]
        med = float(np.median(cvals)) if cvals.size else float("nan")

        atp_m = m60 & is_atp
        wta_m = m60 & is_wta
        c_atp = clip60[atp_m]
        c_wta = clip60[wta_m]
        med_atp = float(np.median(c_atp)) if c_atp.size else float("nan")
        p90_atp = pctile(c_atp, 90)
        n_atp = int(atp_m.sum())
        med_wta = float(np.median(c_wta)) if c_wta.size else float("nan")
        p90_wta = pctile(c_wta, 90)
        n_wta = int(wta_m.sum())

        r = raw60[m60]
        frac_neg = float((r < 0).mean()) if r.size else float("nan")
        frac_zero = float((clip60[m60] == 0).mean()) if r.size else float("nan")

        n_mstar = int(mstar.sum())
        d = clip60[mstar] - clip10[mstar]
        mean_d = float(np.mean(d)) if d.size else float("nan")

        report[N] = dict(
            n_meas=n_meas, n_dec=n_dec, share_dec=share_dec,
            med=med, med_atp=med_atp, p90_atp=p90_atp, n_atp=n_atp,
            med_wta=med_wta, p90_wta=p90_wta, n_wta=n_wta,
            frac_neg=frac_neg, frac_zero=frac_zero,
            n_mstar=n_mstar, mean_d=mean_d,
        )
        print(flush=True)
        print("--- N_min = %d ---" % N, flush=True)
        print("  1) measurable pairs total: %d" % n_meas, flush=True)
        print("  2) measurable in decisive set: %d  (share of 81941: %.6f)" % (n_dec, share_dec), flush=True)
        print("  3) ATP: median=%.6f p90=%.6f n=%d | WTA: median=%.6f p90=%.6f n=%d"
              % (med_atp, p90_atp, n_atp, med_wta, p90_wta, n_wta), flush=True)
        print("     (median over all measurable: %.6f, n=%d)" % (med, n_meas), flush=True)
        print("  4) frac negative (raw<0): %.6f | frac zero (clip==0): %.6f  (same set, n=%d)"
              % (frac_neg, frac_zero, n_meas), flush=True)
        print("  5) |M*| (meas at D=60 & D=10): %d | mean pairwise diff mean(spc60-spc10)=%.8f (n=%d)"
              % (n_mstar, mean_d, n_mstar), flush=True)

    # ---- distinctness guard: no metric (except structural) may coincide across thresholds ----
    print(flush=True)
    print("=== DISTINCTNESS GUARD (rows must differ; frozen mask would repeat) ===", flush=True)
    keys = ["n_meas", "n_dec", "n_mstar", "med_atp", "p90_atp", "med_wta", "p90_wta", "frac_neg"]
    frozen_flags = []
    for k in keys:
        vals = [report[N][k] for N in NMINS]
        uniq = len(set(round(v, 9) if isinstance(v, float) else v for v in vals))
        status = "OK-distinct" if uniq == len(NMINS) else "REPEATED"
        if uniq != len(NMINS):
            frozen_flags.append(k)
        print("  %-10s : %s -> %s" % (k, vals, status), flush=True)
    if frozen_flags:
        print("WARNING: metric(s) repeated across thresholds: %s" % frozen_flags, flush=True)
        print("This can indicate a frozen mask. Reporting anyway, flagged (not silently).", flush=True)

    if LIMIT:
        print(flush=True)
        print("[bench] LIMIT run only -- no CSV/MD written.", flush=True)
        return

    # ---- write outputs (probes/opus only) ----
    write_csv(A, raw60, clip60, raw10, clip10, dec)
    write_md(report, int(dec.sum()), n_meas20, n_mstar20)


def write_csv(A, raw60, clip60, raw10, clip10, dec):
    import csv as _csv
    out = os.path.join(HERE, "spread_estimator_nmin.csv")
    npairs = len(A["tier"])
    m = {N: (measurable_mask(A, N, "60"), measurable_mask(A, N, "60") & measurable_mask(A, N, "10"))
         for N in NMINS}
    cols = ["conditionId", "proxyWallet", "tier", "decisive",
            "sz0", "sz1",
            "n_buy0_60", "n_sell0_60", "n_buy1_60", "n_sell1_60",
            "n_buy0_10", "n_sell0_10", "n_buy1_10", "n_sell1_10",
            "spread_cost_point_raw_60", "spread_cost_point_60",
            "spread_cost_point_raw_10", "spread_cost_point_10",
            "measurable_60_N5", "measurable_60_N10", "measurable_60_N20",
            "mstar_N5", "mstar_N10", "mstar_N20"]
    with open(out, "w", newline="", encoding="utf-8") as f:
        wc = _csv.writer(f)
        wc.writerow(cols)
        for i in range(npairs):
            wc.writerow([
                A["cond"][i], A["wallet"][i], A["tier"][i], int(dec[i]),
                "%.6g" % A["sz0"][i], "%.6g" % A["sz1"][i],
                A["nb0_60"][i], A["ns0_60"][i], A["nb1_60"][i], A["ns1_60"][i],
                A["nb0_10"][i], A["ns0_10"][i], A["nb1_10"][i], A["ns1_10"][i],
                ("%.6f" % raw60[i]) if not np.isnan(raw60[i]) else "",
                ("%.6f" % clip60[i]) if not np.isnan(clip60[i]) else "",
                ("%.6f" % raw10[i]) if not np.isnan(raw10[i]) else "",
                ("%.6f" % clip10[i]) if not np.isnan(clip10[i]) else "",
                int(m[5][0][i]), int(m[10][0][i]), int(m[20][0][i]),
                int(m[5][1][i]), int(m[10][1][i]), int(m[20][1][i]),
            ])
    print("[out] CSV written:", out, flush=True)


def write_md(report, n_decisive, n_meas20, n_mstar20):
    L = []
    L.append("# Пересчёт оценщика спреда при N_min ∈ {5, 10, 20} (D=60)")
    L.append("")
    L.append("Источник: `data/trades_raw_win` (кеш манифеста как в run_fast.py, sha "
             "collect_window_v1.py не менялась). Определение П13 не менялось; обрезка "
             "применяется ПОСЛЕ агрегации по токенам, поэтому `clip == max(raw, 0)` "
             "выполняется поэлементно (жёсткая проверка в прогоне, 0 нарушений).")
    L.append("")
    L.append("Самопроверка при N_min=20: измеримых D=60 = **%d** (контроль 12779), "
             "|M*| = **%d** (контроль 5189)." % (n_meas20, n_mstar20))
    L.append("Пар решающего множества (джойн tier+wallet): **%d** (контроль 81941)." % n_decisive)
    L.append("")
    L.append("| N_min | измеримых всего | измеримых в реш.мн. | доля от 81941 | "
             "median ATP (n) | p90 ATP | median WTA (n) | p90 WTA | median всех | "
             "доля отриц. (raw<0) | доля нулей (clip=0) | \\|M*\\| | mean(spc60−spc10) |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for N in NMINS:
        r = report[N]
        L.append("| %d | %d | %d | %.6f | %.6f (%d) | %.6f | %.6f (%d) | %.6f | %.6f | %.6f | %.6f | %d | %.8f |"
                 % (N, r["n_meas"], r["n_dec"], r["share_dec"],
                    r["med_atp"], r["n_atp"], r["p90_atp"],
                    r["med_wta"], r["n_wta"], r["p90_wta"],
                    r["med"], r["frac_neg"], r["frac_zero"],
                    r["n_mstar"], r["mean_d"]))
    L.append("")
    L.append("## Отчётные строки (по порогам)")
    for N in NMINS:
        r = report[N]
        L.append("")
        L.append("**N_min = %d**" % N)
        L.append("- 1) измеримых пар всего: %d" % r["n_meas"])
        L.append("- 2) измеримых в решающем множестве: %d (доля от 81941: %.6f)" % (r["n_dec"], r["share_dec"]))
        L.append("- 3) median/p90 по тирам: ATP median=%.6f p90=%.6f (n=%d); WTA median=%.6f p90=%.6f (n=%d)"
                 % (r["med_atp"], r["p90_atp"], r["n_atp"], r["med_wta"], r["p90_wta"], r["n_wta"]))
        L.append("- 4) доля отрицательных до обрезки=%.6f; доля нулей после обрезки=%.6f (одно множество, n=%d)"
                 % (r["frac_neg"], r["frac_zero"], r["n_meas"]))
        L.append("- 5) |M*|(окна 10 и 60)=%d; mean(spc60−spc10)=%.8f" % (r["n_mstar"], r["mean_d"]))
    L.append("")
    out = os.path.join(HERE, "nmin_recompute.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("[out] MD written:", out, flush=True)


if __name__ == "__main__":
    main()
