#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# power_inputs2.py -- clustered SE (via existing funnel_a) + selection shrinkage.
# Calls the project implementation; no local clustering code. ASCII console only.

import json, os, sys, math
import numpy as np
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from src.validate.funnel_a import (accumulate_pairs, filter1_pass, screen_one,
                                   iter_artifact_pairs, TIERS, MIN_MATCHES, MIN_DAYS)

ART = ROOT / "data" / "collect_window_2026-02-01_2026-04-28.json"
PASSERS = ROOT / "probes" / "deepseek" / "splithalf_passers.json"
R_TIER = {"atp": 0.6474, "wta": 0.6104}     # FUNNEL_A_SPLITHALF.md lines 35-36
T_TARGET = 3.0

print("[env] python", sys.version.split()[0], "numpy", np.__version__, flush=True)
print("[in] artifact:", ART, flush=True)
print("[in] using funnel_a: accumulate_pairs / filter1_pass / screen_one (no local clustering)", flush=True)
print("[cfg] MIN_MATCHES=%s MIN_DAYS=%s TIERS=%s" % (MIN_MATCHES, MIN_DAYS, TIERS), flush=True)
print("[cfg] split-half r: atp=%.4f wta=%.4f" % (R_TIER["atp"], R_TIER["wta"]), flush=True)

f1, f5, total = accumulate_pairs(iter_artifact_pairs(ART))
print("[in] pairs accumulated:", total, flush=True)
f1p, f1detail = filter1_pass(f1)
for t in TIERS:
    print("[f1] tier=%s candidates passing filter 1: %d" % (t, len(f1p[t])), flush=True)

# ---- mean_tier over ALL filter-1 candidates (not only screen passers) ----
mean_tier = {}
for t in TIERS:
    ms = []
    for w in sorted(f1p[t]):
        r = f5[t].get(w)
        if r and r["clv"]:
            ms.append(sum(r["clv"]) / len(r["clv"]))
    a = np.asarray(ms, dtype=float)
    mean_tier[t] = float(a.mean())
    print("[mean_tier] tier=%s n_candidates=%d mean_of_wallet_means=%.9g (median=%.9g min=%.9g max=%.9g)"
          % (t, a.size, a.mean(), np.median(a), a.min(), a.max()), flush=True)

# ---- the 14 candidate tier-rows ----
P = json.load(open(PASSERS, encoding="utf-8"))
want = [(t, r["wallet"].strip().lower()) for t, rows in P["tiers"].items() for r in rows]
print("[in] candidate tier-rows:", len(want), "distinct wallets:", len({w for _, w in want}), flush=True)

rows = []
for tier, w in sorted(want):
    r = f5[tier][w]
    clv = r["clv"]
    n = len(clv)
    sr = screen_one(clv, r["cond"], r["day"])
    v = np.asarray(clv, dtype=float)
    sd = float(v.std(ddof=1))
    se_naive = sd / math.sqrt(n)
    se_cl = float(sr.se_edge)
    ratio = se_cl / se_naive
    # per-pair (unit) clustered dispersion, so n can be re-solved
    se_unit_cl = se_cl * math.sqrt(n)
    edge = float(sr.mean_clv)
    edge_exp = mean_tier[tier] + R_TIER[tier] * (edge - mean_tier[tier])

    n_naive = (T_TARGET * sd / edge) ** 2 if edge > 0 else None
    n_clust = (T_TARGET * se_unit_cl / edge) ** 2 if edge > 0 else None
    n_shrunk = (T_TARGET * sd / edge_exp) ** 2 if edge_exp > 0 else None
    n_both = (T_TARGET * se_unit_cl / edge_exp) ** 2 if edge_exp > 0 else None

    rows.append(dict(tier=tier, wallet=w, n=n, mean_clv=edge, sd_clv=sd,
                     se_naive=se_naive, se_match=float(sr.se_match), se_day=float(sr.se_day),
                     se_cgm=float(sr.se_cgm), se_clustered=se_cl, se_winner=sr.se_winner,
                     se_ratio=ratio, t_naive=edge / se_naive, t_clustered=float(sr.t),
                     se_unit_clustered=se_unit_cl,
                     mean_tier=mean_tier[tier], r_tier=R_TIER[tier], edge_expected=edge_exp,
                     shrink_factor=edge_exp / edge if edge > 0 else None,
                     n_naive=n_naive, n_clustered=n_clust, n_shrunk=n_shrunk, n_both=n_both))
    print("[calc] %s %s n=%4d edge=%.6g edge_exp=%.6g se_naive=%.6g se_cl=%.6g ratio=%.4f win=%-5s t_cl=%.4g n_naive=%.4g n_cl=%.4g n_shr=%.4g n_both=%.4g"
          % (tier, w[:10], n, edge, edge_exp, se_naive, se_cl, ratio, sr.se_winner, sr.t,
             n_naive, n_clust, n_shrunk, n_both), flush=True)

# ---- summaries ----
g = lambda k: np.array([r[k] for r in rows if r[k] is not None], dtype=float)
print("", flush=True)
print("=== SE ratio clustered/naive ===", flush=True)
rt = g("se_ratio")
print("  median=%.6g min=%.6g p25=%.6g p75=%.6g max=%.6g" % (np.median(rt), rt.min(), np.percentile(rt, 25), np.percentile(rt, 75), rt.max()), flush=True)
from collections import Counter
print("  winning SE variant:", dict(Counter(r["se_winner"] for r in rows)), flush=True)
print("  rows where clustered SE is LARGER than naive: %d of %d" % (int((rt > 1).sum()), rt.size), flush=True)

print("", flush=True)
print("=== SHRINKAGE ===", flush=True)
sf = g("shrink_factor")
print("  edge_expected/edge_observed: median=%.6g min=%.6g max=%.6g" % (np.median(sf), sf.min(), sf.max()), flush=True)
for t in TIERS:
    sub = [r for r in rows if r["tier"] == t]
    print("  tier=%s mean_tier=%.9g r=%.4f  observed edge med=%.6g -> expected med=%.6g"
          % (t, mean_tier[t], R_TIER[t],
             float(np.median([r["mean_clv"] for r in sub])),
             float(np.median([r["edge_expected"] for r in sub]))), flush=True)

print("", flush=True)
print("=== REQUIRED n for t=3, four variants ===", flush=True)
for k in ("n_naive", "n_clustered", "n_shrunk", "n_both"):
    a = g(k)
    print("  %-12s median=%10.6g min=%10.6g max=%10.6g  (rows=%d)" % (k, np.median(a), a.min(), a.max(), a.size), flush=True)

LIVE = ["0xde9f7f4e", "0xa509ae94", "0xdbdd4515", "0x204f72f3", "0x9663a1bc", "0xfbe49f06", "0x70f96881", "0x3d7817cc"]
print("", flush=True)
print("=== EIGHT LIVE WALLETS: final required n (n_both) ===", flush=True)
live_rows = []
for pref in LIVE:
    rs = [r for r in rows if r["wallet"].startswith(pref)]
    for r in rs:
        print("  %-10s %-3s n_window=%4d n_both=%10.6g  (n_naive=%.6g)" % (pref, r["tier"], r["n"], r["n_both"], r["n_naive"]), flush=True)
    tot = sum(r["n_both"] for r in rs if r["n_both"] is not None)
    live_rows.append(dict(wallet_prefix=pref, tier_rows=len(rs),
                          n_both_by_tier={r["tier"]: r["n_both"] for r in rs},
                          n_both_min=min(r["n_both"] for r in rs),
                          n_window_by_tier={r["tier"]: r["n"] for r in rs}))
lb = np.array([x["n_both_min"] for x in live_rows], dtype=float)
print("  across 8 live wallets, best-tier n_both: median=%.6g min=%.6g max=%.6g" % (np.median(lb), lb.min(), lb.max()), flush=True)

out = dict(
    method=dict(
        se_clustered="src/validate/funnel_a.py screen_one -> se_edge = max(se_match, se_day, se_cgm); project implementation called, not reimplemented",
        se_unit_clustered="se_clustered * sqrt(n_i), clustered dispersion expressed per pair",
        mean_tier="mean of per-wallet mean clv over ALL filter-1 candidates in the tier (atp/wta), not only screen passers",
        edge_expected="mean_tier + r * (edge_i - mean_tier)",
        r_source="probes/deepseek/FUNNEL_A_SPLITHALF.md lines 35-36",
        n_formulas=dict(n_naive="(3*sd/edge)^2", n_clustered="(3*se_unit_clustered/edge)^2",
                        n_shrunk="(3*sd/edge_expected)^2", n_both="(3*se_unit_clustered/edge_expected)^2"),
    ),
    mean_tier=mean_tier,
    r_tier=R_TIER,
    filter1_candidates={t: len(f1p[t]) for t in TIERS},
    summary=dict(
        se_ratio_median=float(np.median(rt)), se_ratio_min=float(rt.min()), se_ratio_max=float(rt.max()),
        se_winner_counts=dict(Counter(r["se_winner"] for r in rows)),
        shrink_factor_median=float(np.median(sf)),
        n_naive_median=float(np.median(g("n_naive"))), n_clustered_median=float(np.median(g("n_clustered"))),
        n_shrunk_median=float(np.median(g("n_shrunk"))), n_both_median=float(np.median(g("n_both"))),
        n_both_min=float(g("n_both").min()), n_both_max=float(g("n_both").max()),
    ),
    rows=rows,
    live_wallets=live_rows,
)
with open(HERE / "power_inputs_clustered.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=1, ensure_ascii=False, allow_nan=True)
print("", flush=True)
print("[out] power_inputs_clustered.json written", flush=True)
