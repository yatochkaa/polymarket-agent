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
NMINS = (1, 2, 3, 5, 10, 20)

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


def pool_sizes(A, suf):
    """Pool trade count per pair for window suffix suf = sum over ACTIVE tokens of (n_buy+n_sell)."""
    a0 = A["sz0"] > 0
    a1 = A["sz1"] > 0
    p0 = np.where(a0, A["nb0_%s" % suf] + A["ns0_%s" % suf], 0)
    p1 = np.where(a1, A["nb1_%s" % suf] + A["ns1_%s" % suf], 0)
    return (p0 + p1).astype(np.int64)


def measurable_mask(A, N, suf):
    """Per-token AND at threshold N for window suffix suf in {'60','10'}."""
    a0 = A["sz0"] > 0
    a1 = A["sz1"] > 0
    ok0 = (~a0) | ((A["nb0_%s" % suf] >= N) & (A["ns0_%s" % suf] >= N))
    ok1 = (~a1) | ((A["nb1_%s" % suf] >= N) & (A["ns1_%s" % suf] >= N))
    return ok0 & ok1


def aggregate(A, suf):
    """CHANGE 1 (P13 definition, correct order): eff_spread clip is applied
    INSIDE each token, size-weighted averaging comes AFTER.

      raw(w,m,D)  = sum_a S_a * raw_a / sum_a S_a                 (signed, unclipped)
      clip(w,m,D) = sum_a S_a * max(raw_a, 0) / sum_a S_a         (clip-then-average)

    Returns (raw, clip_new, clip_old) where clip_old = max(raw,0) is the previous
    (wrong-order) average-then-clip variant, kept only for the side-by-side
    comparison required by the task. Nothing downstream uses clip_old for reporting.
    """
    a0 = (A["sz0"] > 0).astype(float)
    a1 = (A["sz1"] > 0).astype(float)
    w0 = a0 * A["sz0"]
    w1 = a1 * A["sz1"]
    # raw_a may be nan for active-but-degenerate tokens; nan then propagates to the
    # pair value, which is correct (such a pair is never measurable at N>=1).
    r0 = np.where(a0 > 0, A["rw0_%s" % suf], 0.0)
    r1 = np.where(a1 > 0, A["rw1_%s" % suf], 0.0)
    den = w0 + w1
    with np.errstate(invalid="ignore", divide="ignore"):
        raw = (w0 * r0 + w1 * r1) / den
        clip_new = (w0 * np.maximum(r0, 0.0) + w1 * np.maximum(r1, 0.0)) / den
    clip_old = np.maximum(raw, 0.0)
    return raw, clip_new, clip_old


def pctile(x, p):
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if x.size == 0:
        return float("nan")
    return float(np.percentile(x, p))


def fmt(v):
    return "%.9g" % v if isinstance(v, float) else str(v)


def main():
    target = os.path.join(ROOT, "collect_window_v1.py")
    sha = hashlib.sha256(open(target, "rb").read()).hexdigest().upper()
    print("[sha256] collect_window_v1.py:", sha, flush=True)
    if sha != SHA_FROZEN:
        print("STOP: SHA256 mismatch (collect_window_v1.py changed)", flush=True)
        sys.exit(2)

    A = collect()
    npairs = len(A["tier"])

    a0 = A["sz0"] > 0
    a1 = A["sz1"] > 0
    two_tok = a0 & a1
    one_tok = (a0 ^ a1)
    print(flush=True)
    print("[tokens] pairs with 1 active token: %d | with 2 active tokens: %d | total %d"
          % (int(one_tok.sum()), int(two_tok.sum()), npairs), flush=True)

    raw60, clip60, clipold60 = aggregate(A, "60")
    raw10, clip10, clipold10 = aggregate(A, "10")

    # ================= CHANGE 1 CHECK: clip-inside-token, average-after =========
    print(flush=True)
    print("=== CHECK 1: clip applied INSIDE token, size-weighted average AFTER ===", flush=True)
    fail = 0
    shown = []
    for suf, raw, clip in (("60", raw60, clip60), ("10", raw10, clip10)):
        r0 = A["rw0_%s" % suf]
        r1 = A["rw1_%s" % suf]
        w0 = np.where(a0, A["sz0"], 0.0)
        w1 = np.where(a1, A["sz1"], 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            exp_two = (w0 * np.maximum(r0, 0.0) + w1 * np.maximum(r1, 0.0)) / (w0 + w1)
        exp_one = np.where(a0, np.maximum(r0, 0.0), np.maximum(r1, 0.0))
        defined = ~np.isnan(raw)
        bad1 = defined & one_tok & (~np.isclose(clip, exp_one, atol=1e-12, rtol=0.0))
        bad2 = defined & two_tok & (~np.isclose(clip, exp_two, atol=1e-12, rtol=0.0))
        print("  D=%s: defined=%d | 1-token rows=%d viol=%d | 2-token rows=%d viol=%d"
              % (suf, int(defined.sum()), int((defined & one_tok).sum()), int(bad1.sum()),
                 int((defined & two_tok).sum()), int(bad2.sum())), flush=True)
        fail += int(bad1.sum()) + int(bad2.sum())
        for i in np.flatnonzero(bad1 | bad2)[:10]:
            shown.append((suf, A["cond"][i], A["wallet"][i], float(raw[i]), float(clip[i])))
    if fail:
        print("CHECK 1 FAILED: %d rows. First 10 violators:" % fail, flush=True)
        for suf, cond, wal, r, c in shown[:10]:
            print("  D=%s conditionId=%s proxyWallet=%s raw=%.9g clip=%.9g" % (suf, cond, wal, r, c), flush=True)
        sys.exit(5)
    print("  OK: clip == max(raw,0) on 1-token pairs and == sum(w*max(raw_a,0))/sum(w) on 2-token pairs.", flush=True)

    # how many 2-token pairs change vs old (average-then-clip) order
    print(flush=True)
    print("=== CHANGE 1 IMPACT (new clip-inside vs old clip-after) ===", flush=True)
    for suf, clip, cold in (("60", clip60, clipold60), ("10", clip10, clipold10)):
        defined = ~np.isnan(clip)
        diff = defined & (~np.isclose(clip, cold, atol=0.0, rtol=0.0))
        print("  D=%s: 2-token pairs=%d | rows where new clip != old clip=%d (all 2-token: %s) | max |delta|=%.9g"
              % (suf, int((two_tok & defined).sum()), int(diff.sum()),
                 bool((diff & ~two_tok).sum() == 0),
                 float(np.nanmax(np.abs(clip[defined] - cold[defined]))) if defined.sum() else float("nan")), flush=True)

    # ================= CHANGE 2: pool sizes + band guard =======================
    pool60 = pool_sizes(A, "60")
    pool10 = pool_sizes(A, "10")
    print(flush=True)
    print("=== CHANGE 2: pool sizes per pair (D=10 / D=60) ===", flush=True)
    print("  pool10: min=%d max=%d sum=%d | pool60: min=%d max=%d sum=%d | pairs with pool60<pool10 (must be 0): %d"
          % (int(pool10.min()), int(pool10.max()), int(pool10.sum()),
             int(pool60.min()), int(pool60.max()), int(pool60.sum()),
             int((pool60 < pool10).sum())), flush=True)

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
    n_decisive = int(dec.sum())
    print("[decisive] decisive-set pairs (control 81941):", n_decisive, flush=True)

    tier = A["tier"]
    is_atp = tier == "atp"
    is_wta = tier == "wta"

    # ---- CONTROL at N_min=20 ----
    m60_20 = measurable_mask(A, 20, "60")
    m10_20 = measurable_mask(A, 20, "10")
    n_meas20 = int(m60_20.sum())
    n_mstar20 = int((m60_20 & m10_20).sum())
    print(flush=True)
    print("=== CONTROL (N_min=20) ===", flush=True)
    print("  measurable D=60: %d (control 12779) -> %s" % (n_meas20, "OK" if n_meas20 == 12779 else "MISMATCH"), flush=True)
    print("  |M*|: %d (control 5189) -> %s" % (n_mstar20, "OK" if n_mstar20 == 5189 else "MISMATCH"), flush=True)
    print("  decisive set: %d (control 81941) -> %s" % (n_decisive, "OK" if n_decisive == 81941 else "MISMATCH"), flush=True)
    if not LIMIT and (n_meas20 != 12779 or n_mstar20 != 5189 or n_decisive != 81941):
        print("STOP: control failed -- not adjusting code to controls, reporting mismatch and exiting.", flush=True)
        sys.exit(6)

    # ================= PER-THRESHOLD REPORT ====================================
    print(flush=True)
    print("=== PER-THRESHOLD REPORT (six thresholds; D=60 unless stated) ===", flush=True)
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
        med_wta = float(np.median(c_wta)) if c_wta.size else float("nan")
        p90_atp = pctile(c_atp, 90)
        p90_wta = pctile(c_wta, 90)
        n_atp = int(atp_m.sum())
        n_wta = int(wta_m.sum())

        # old-order versions (side-by-side, CHANGE 1 impact)
        o_atp = clipold60[atp_m]
        o_wta = clipold60[wta_m]
        omed_atp = float(np.median(o_atp)) if o_atp.size else float("nan")
        omed_wta = float(np.median(o_wta)) if o_wta.size else float("nan")
        op90_atp = pctile(o_atp, 90)
        op90_wta = pctile(o_wta, 90)
        n_two_meas = int((m60 & two_tok).sum())
        n_two_changed = int((m60 & two_tok & (~np.isclose(clip60, clipold60, atol=0.0, rtol=0.0))).sum())

        r = raw60[m60]
        frac_neg = float((r < 0).mean()) if r.size else float("nan")
        n_neg = int((r < 0).sum())
        frac_zero = float((clip60[m60] == 0).mean()) if r.size else float("nan")
        n_zero = int((clip60[m60] == 0).sum())

        n_mstar = int(mstar.sum())
        med60_ms = float(np.median(clip60[mstar])) if n_mstar else float("nan")
        med10_ms = float(np.median(clip10[mstar])) if n_mstar else float("nan")
        ratio_ms = (med60_ms / med10_ms) if (n_mstar and med10_ms != 0) else float("nan")

        # CHANGE 2 guard on this threshold's measurable set
        n_pool_diff = int((m60 & (pool60 > pool10)).sum())
        n_band = int((pool60[m60] - pool10[m60]).sum())
        n_band_pairs = int(((pool60 - pool10) > 0)[m60].sum())
        guard_ok = (n_pool_diff == n_band)

        report[N] = dict(
            n_meas=n_meas, n_dec=n_dec, share_dec=share_dec, med=med,
            med_atp=med_atp, p90_atp=p90_atp, n_atp=n_atp,
            med_wta=med_wta, p90_wta=p90_wta, n_wta=n_wta,
            omed_atp=omed_atp, op90_atp=op90_atp, omed_wta=omed_wta, op90_wta=op90_wta,
            n_two_meas=n_two_meas, n_two_changed=n_two_changed,
            frac_neg=frac_neg, n_neg=n_neg, frac_zero=frac_zero, n_zero=n_zero,
            n_mstar=n_mstar, med60_ms=med60_ms, med10_ms=med10_ms, ratio_ms=ratio_ms,
            n_pool_diff=n_pool_diff, n_band=n_band, n_band_pairs=n_band_pairs, guard_ok=guard_ok,
        )
        print(flush=True)
        print("--- N_min = %d ---" % N, flush=True)
        print("  1) measurable pairs total: %d" % n_meas, flush=True)
        print("  2) measurable in decisive set: %d | share of 81941: %s" % (n_dec, fmt(share_dec)), flush=True)
        print("  3) ATP: n=%d median=%s p90=%s | WTA: n=%d median=%s p90=%s | all: n=%d median=%s"
              % (n_atp, fmt(med_atp), fmt(p90_atp), n_wta, fmt(med_wta), fmt(p90_wta), n_meas, fmt(med)), flush=True)
        print("  4) frac raw<0 = %s (%d/%d) | frac clip==0 = %s (%d/%d)  [same set]"
              % (fmt(frac_neg), n_neg, n_meas, fmt(frac_zero), n_zero, n_meas), flush=True)
        print("  5) |M*| = %d | median(spc60|M*) = %s | median(spc10|M*) = %s | ratio = %s"
              % (n_mstar, fmt(med60_ms), fmt(med10_ms), fmt(ratio_ms)), flush=True)
        print("  6) n_pool_diff = %d | n_band (trades in D10..D60 band) = %d | guard %s"
              % (n_pool_diff, n_band, "MATCHED" if guard_ok else "NOT MATCHED (reported as-is, not adjusted)"), flush=True)
        print("     band diagnostics: pairs with >=1 band trade = %d (equals n_pool_diff: %s) | band trades per such pair: mean=%s max=%d"
              % (n_band_pairs, n_band_pairs == n_pool_diff,
                 fmt(n_band / n_band_pairs) if n_band_pairs else "n/a",
                 int((pool60 - pool10)[m60].max()) if n_meas else 0), flush=True)
        print("     old-order clip (for CHANGE 1 comparison): ATP median=%s p90=%s | WTA median=%s p90=%s | 2-token measurable=%d changed=%d"
              % (fmt(omed_atp), fmt(op90_atp), fmt(omed_wta), fmt(op90_wta), n_two_meas, n_two_changed), flush=True)

    # ================= DISTINCTNESS GUARD ======================================
    print(flush=True)
    print("=== DISTINCTNESS GUARD (six rows must differ; frozen/copied mask would repeat) ===", flush=True)
    keys = ["n_meas", "n_dec", "n_mstar", "n_pool_diff", "med_atp", "p90_atp", "med_wta", "p90_wta", "frac_neg"]
    repeated = []
    for k in keys:
        vals = [report[N][k] for N in NMINS]
        uniq = len(set(round(v, 12) if isinstance(v, float) else v for v in vals))
        st = "OK-distinct" if uniq == len(NMINS) else "REPEATED(%d/%d uniq)" % (uniq, len(NMINS))
        if uniq != len(NMINS):
            repeated.append(k)
        print("  %-12s: %s -> %s" % (k, [fmt(v) for v in vals], st), flush=True)
    if repeated:
        print("NOTE: repeated metric(s): %s -- listed above with values; masks were recomputed per threshold (see n_meas)." % repeated, flush=True)

    # ================= TIER MEDIAN TABLE + 0.005 DEPARTURE =====================
    print(flush=True)
    print("=== TIER MEDIAN TABLE (all six thresholds) ===", flush=True)
    print("  N_min | n_atp | median_atp | n_wta | median_wta | max|median-0.005|", flush=True)
    dep = None
    for N in NMINS:
        r = report[N]
        d = max(abs(r["med_atp"] - 0.005), abs(r["med_wta"] - 0.005))
        print("  %5d | %5d | %-12s | %5d | %-12s | %s" % (N, r["n_atp"], fmt(r["med_atp"]), r["n_wta"], fmt(r["med_wta"]), fmt(d)), flush=True)
        if dep is None and d > 0.0005:
            dep = (N, d)
    if dep:
        print("  smallest threshold where a tier median departs from 0.005 by more than 0.0005: N_min=%d (max deviation %s)" % dep, flush=True)
    else:
        print("  no threshold among %s has a tier median departing from 0.005 by more than 0.0005" % (list(NMINS),), flush=True)

    if LIMIT:
        print(flush=True)
        print("[bench] LIMIT run only -- no CSV/MD written.", flush=True)
        return

    write_csv(A, raw60, clip60, clipold60, raw10, clip10, clipold10, pool60, pool10, dec)
    write_md(report, n_decisive, n_meas20, n_mstar20, npairs,
             int(one_tok.sum()), int(two_tok.sum()), repeated, dep)


def write_csv(A, raw60, clip60, clipold60, raw10, clip10, clipold10, pool60, pool10, dec):
    import csv as _csv
    out = os.path.join(HERE, "spread_estimator_nmin_v2.csv")
    if os.path.exists(out):
        print("STOP: refusing to overwrite existing %s" % out, flush=True)
        sys.exit(7)
    npairs = len(A["tier"])
    m = {N: (measurable_mask(A, N, "60"), measurable_mask(A, N, "60") & measurable_mask(A, N, "10"))
         for N in NMINS}
    cols = ["conditionId", "proxyWallet", "tier", "decisive",
            "sz0", "sz1", "n_active_tokens",
            "n_buy0_60", "n_sell0_60", "n_buy1_60", "n_sell1_60",
            "n_buy0_10", "n_sell0_10", "n_buy1_10", "n_sell1_10",
            "pool_size_60", "pool_size_10", "pool_band_60_10",
            "spread_cost_point_raw_60", "spread_cost_point_60", "spread_cost_point_60_oldorder",
            "spread_cost_point_raw_10", "spread_cost_point_10", "spread_cost_point_10_oldorder"]
    cols += ["measurable_60_N%d" % N for N in NMINS]
    cols += ["mstar_N%d" % N for N in NMINS]
    g = lambda v: ("%.9g" % v) if not np.isnan(v) else ""
    with open(out, "w", newline="", encoding="utf-8") as f:
        wc = _csv.writer(f)
        wc.writerow(cols)
        nact = ((A["sz0"] > 0).astype(int) + (A["sz1"] > 0).astype(int))
        band = pool60 - pool10
        for i in range(npairs):
            row = [A["cond"][i], A["wallet"][i], A["tier"][i], int(dec[i]),
                   "%.9g" % A["sz0"][i], "%.9g" % A["sz1"][i], int(nact[i]),
                   A["nb0_60"][i], A["ns0_60"][i], A["nb1_60"][i], A["ns1_60"][i],
                   A["nb0_10"][i], A["ns0_10"][i], A["nb1_10"][i], A["ns1_10"][i],
                   int(pool60[i]), int(pool10[i]), int(band[i]),
                   g(raw60[i]), g(clip60[i]), g(clipold60[i]),
                   g(raw10[i]), g(clip10[i]), g(clipold10[i])]
            row += [int(m[N][0][i]) for N in NMINS]
            row += [int(m[N][1][i]) for N in NMINS]
            wc.writerow(row)
    print("[out] CSV written:", out, flush=True)


def write_md(report, n_decisive, n_meas20, n_mstar20, npairs, n_one, n_two, repeated, dep):
    out = os.path.join(HERE, "nmin_recompute_v2.md")
    if os.path.exists(out):
        print("STOP: refusing to overwrite existing %s" % out, flush=True)
        sys.exit(7)
    L = []
    L.append("# Оценщик спреда, версия 2: обрезка внутри токена, guard на пулы, шесть порогов")
    L.append("")
    L.append("Определение П13 не менялось. Источник — `data/trades_raw_win` (только чтение, кеш")
    L.append("манифеста как в run_fast.py). `collect_window_v1.py` не трогался, sha сверена в прогоне.")
    L.append("Все числа ниже — из вывода консоли этого прогона, точность %.9g.")
    L.append("")
    L.append("Контроль при N_min=20: измеримых D=60 = **%d** (контроль 12779), |M*| = **%d** (контроль 5189), "
             "решающее множество = **%d** (контроль 81941)." % (n_meas20, n_mstar20, n_decisive))
    L.append("Пар всего: %d; из них с одним активным токеном %d, с двумя %d." % (npairs, n_one, n_two))
    L.append("")
    L.append("## Развёртка по шести порогам")
    L.append("")
    L.append("| N_min | измеримых | в реш.мн. | доля от 81941 | ATP n | ATP median | ATP p90 | WTA n | WTA median | WTA p90 | доля raw<0 | доля clip=0 | \\|M*\\| | med60/med10 на M* | n_pool_diff |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for N in NMINS:
        r = report[N]
        L.append("| %d | %d | %d | %s | %d | %s | %s | %d | %s | %s | %s | %s | %d | %s | %d |" % (
            N, r["n_meas"], r["n_dec"], fmt(r["share_dec"]),
            r["n_atp"], fmt(r["med_atp"]), fmt(r["p90_atp"]),
            r["n_wta"], fmt(r["med_wta"]), fmt(r["p90_wta"]),
            fmt(r["frac_neg"]), fmt(r["frac_zero"]), r["n_mstar"], fmt(r["ratio_ms"]), r["n_pool_diff"]))
    L.append("")
    for N in NMINS:
        r = report[N]
        L.append("**N_min = %d**" % N)
        L.append("- 1) измеримых пар всего: %d" % r["n_meas"])
        L.append("- 2) измеримых в решающем множестве: %d, доля от 81941 = %s" % (r["n_dec"], fmt(r["share_dec"])))
        L.append("- 3) ATP: n=%d median=%s p90=%s | WTA: n=%d median=%s p90=%s" % (
            r["n_atp"], fmt(r["med_atp"]), fmt(r["p90_atp"]), r["n_wta"], fmt(r["med_wta"]), fmt(r["p90_wta"])))
        L.append("- 4) доля отрицательных raw = %s (%d/%d); доля нулей после обрезки = %s (%d/%d), одно и то же множество" % (
            fmt(r["frac_neg"]), r["n_neg"], r["n_meas"], fmt(r["frac_zero"]), r["n_zero"], r["n_meas"]))
        L.append("- 5) |M*| = %d; median(spc60|M*) = %s; median(spc10|M*) = %s; отношение = %s" % (
            r["n_mstar"], fmt(r["med60_ms"]), fmt(r["med10_ms"]), fmt(r["ratio_ms"])))
        L.append("- 6) n_pool_diff = %d; n_band = %d; guard: %s (пар с хотя бы одной сделкой в полосе: %d)" % (
            r["n_pool_diff"], r["n_band"],
            "сошёлся" if r["guard_ok"] else "НЕ СОШЁЛСЯ (числа приведены как есть, подгонки нет)", r["n_band_pairs"]))
        L.append("")
    L.append("## Таблица медиан по тирам и отход от 0.005")
    L.append("")
    L.append("| N_min | ATP median | WTA median | max\\|median−0.005\\| |")
    L.append("|---|---|---|---|")
    for N in NMINS:
        r = report[N]
        d = max(abs(r["med_atp"] - 0.005), abs(r["med_wta"] - 0.005))
        L.append("| %d | %s | %s | %s |" % (N, fmt(r["med_atp"]), fmt(r["med_wta"]), fmt(d)))
    L.append("")
    if dep:
        L.append("Наименьший порог, где медиана по тиру уходит от 0.005 более чем на 0.0005: **N_min=%d** (отклонение %s)." % (dep[0], fmt(dep[1])))
    else:
        L.append("Ни на одном из порогов %s медиана по тиру не уходит от 0.005 более чем на 0.0005." % (list(NMINS),))
    L.append("")
    L.append("## Двухтокенные пары: новая обрезка против старой")
    L.append("")
    L.append("Всего пар с двумя активными токенами: **%d** (из %d)." % (n_two, npairs))
    L.append("")
    L.append("| N_min | 2-токенных измеримых | из них clip изменился | ATP median нов./стар. | ATP p90 нов./стар. | WTA median нов./стар. | WTA p90 нов./стар. |")
    L.append("|---|---|---|---|---|---|---|")
    for N in NMINS:
        r = report[N]
        L.append("| %d | %d | %d | %s / %s | %s / %s | %s / %s | %s / %s |" % (
            N, r["n_two_meas"], r["n_two_changed"],
            fmt(r["med_atp"]), fmt(r["omed_atp"]), fmt(r["p90_atp"]), fmt(r["op90_atp"]),
            fmt(r["med_wta"]), fmt(r["omed_wta"]), fmt(r["p90_wta"]), fmt(r["op90_wta"])))
    L.append("")
    L.append("## Различность строк")
    L.append("")
    if repeated:
        L.append("Повторившиеся показатели между порогами: %s. Маски пересчитываются на каждом пороге "
                 "(см. различные n_meas), повтор приведён явно, а не сглажен." % repeated)
    else:
        L.append("Все девять контролируемых показателей различны на всех шести порогах — маска пересчитывается, а не копируется.")
    L.append("")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print("[out] MD written:", out, flush=True)


if __name__ == "__main__":
    main()
