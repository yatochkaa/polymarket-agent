#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# power_group.py -- GROUP hypothesis power: pooled sample of the 8 live wallets.
# One test, no multiplicity. Clustering must work ACROSS wallets: one match with
# pairs from two candidates is ONE cluster, not two.
# ASCII console output only.

import json, os, sys, math
import numpy as np
from pathlib import Path

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
from src.validate.funnel_a import (accumulate_pairs, filter1_pass, iter_artifact_pairs,
                                   cgm_double_cluster_se, day_from_slug, TIERS)
from pm.stats import cluster_mean_se

ART = ROOT / "data" / "collect_window_2026-02-01_2026-04-28.json"
R_TIER = {"atp": 0.6474, "wta": 0.6104}
LIVE = ["0xde9f7f4e", "0xa509ae94", "0xdbdd4515", "0x204f72f3",
        "0x9663a1bc", "0xfbe49f06", "0x70f96881", "0x3d7817cc"]
SPAN_DAYS = 86          # 2026-02-01 .. 2026-04-28
print("[env] python %s numpy %s" % (sys.version.split()[0], np.__version__), flush=True)
print("[cfg] group of %d wallets, both tiers pooled, one test, no BH" % len(LIVE), flush=True)
print("[cfg] clustering via pm.stats.cluster_mean_se and funnel_a.cgm_double_cluster_se", flush=True)

# ---- mean_tier over ALL filter-1 candidates (same rule as before) ----
f1, f5, total = accumulate_pairs(iter_artifact_pairs(ART))
f1p, _ = filter1_pass(f1)
mean_tier = {}
for t in TIERS:
    ms = [sum(f5[t][w]["clv"]) / len(f5[t][w]["clv"])
          for w in sorted(f1p[t]) if f5[t].get(w) and f5[t][w]["clv"]]
    mean_tier[t] = float(np.mean(ms))
    print("[mean_tier] %s n=%d mean=%.9g" % (t, len(ms), mean_tier[t]), flush=True)

# ---- pooled sample ----
d = json.load(open(ART, encoding="utf-8"))
sel = [p for p in d["pairs"] if any(p["wallet"].startswith(x) for x in LIVE)]
print("", flush=True)
print("=== 1. POOLED SAMPLE ===", flush=True)
print("  pairs: %d | distinct wallets: %d | window span: %d days" % (len(sel), len({p["wallet"] for p in sel}), SPAN_DAYS), flush=True)
rate = len(sel) / SPAN_DAYS
print("  pairs per day: %.6g" % rate, flush=True)
for t in TIERS:
    sub = [p for p in sel if p["tier"] == t]
    print("  tier=%s pairs=%d (%.4g%%)" % (t, len(sub), 100.0 * len(sub) / len(sel)), flush=True)
byw = {}
for p in sel:
    byw[p["wallet"][:10]] = byw.get(p["wallet"][:10], 0) + 1
print("  per wallet:", " ".join("%s=%d" % (k, v) for k, v in sorted(byw.items(), key=lambda x: -x[1])), flush=True)

clv = np.array([p["clv"] for p in sel], dtype=float)
wgt = np.array([abs(p["N"]) for p in sel], dtype=float)         # contracts
conds = [p["cond"] for p in sel]
days = [day_from_slug(p.get("slug") or "") or (p.get("cond") or "?") for p in sel]
wal = [p["wallet"] for p in sel]
# cluster key that does NOT pool across wallets: (wallet, match)
wc = [w + "|" + c for w, c in zip(wal, conds)]
wd = [w + "|" + dd for w, dd in zip(wal, days)]

print("", flush=True)
print("=== 2. EDGE, observed and shrunk ===", flush=True)
obs_mean = float(clv.mean())
shr = np.array([mean_tier[p["tier"]] + R_TIER[p["tier"]] * (p["clv"] - mean_tier[p["tier"]]) for p in sel], dtype=float)
shr_mean = float(shr.mean())
print("  equal-weight observed mean clv : %.9g" % obs_mean, flush=True)
print("  equal-weight shrunk  mean clv : %.9g  (multiplier %.6g)" % (shr_mean, shr_mean / obs_mean), flush=True)
w_obs = float(np.dot(clv, wgt) / wgt.sum())
w_shr = float(np.dot(shr, wgt) / wgt.sum())
print("  size-weighted observed mean    : %.9g" % w_obs, flush=True)
print("  size-weighted shrunk mean      : %.9g  (multiplier %.6g)" % (w_shr, w_shr / w_obs), flush=True)
print("  NOTE: shrinkage applied per pair using its own tier r; group edge is not a single-tier object", flush=True)

print("", flush=True)
print("=== 3. SE WITH AND WITHOUT CROSS-WALLET CLUSTERING ===", flush=True)
# overlap measure: matches containing pairs of more than one candidate
from collections import defaultdict, Counter
per_match = defaultdict(set)
for p in sel:
    per_match[p["cond"]].add(p["wallet"])
multi = {c: ws for c, ws in per_match.items() if len(ws) > 1}
occ = Counter(len(ws) for ws in per_match.values())
print("  distinct matches in pooled sample: %d" % len(per_match), flush=True)
print("  matches with pairs of MORE THAN ONE candidate: %d (%.4g%% of matches)"
      % (len(multi), 100.0 * len(multi) / len(per_match)), flush=True)
print("  candidates per match histogram:", dict(sorted(occ.items())), flush=True)
pairs_in_multi = sum(1 for p in sel if len(per_match[p["cond"]]) > 1)
print("  pairs living in multi-candidate matches: %d (%.4g%% of pairs)"
      % (pairs_in_multi, 100.0 * pairs_in_multi / len(sel)), flush=True)
per_day = defaultdict(set)
for p, dd in zip(sel, days):
    per_day[dd].add(p["wallet"])
print("  distinct tournament days: %d | days with >1 candidate: %d"
      % (len(per_day), sum(1 for ws in per_day.values() if len(ws) > 1)), flush=True)

def se_set(values, c1, c2, label):
    se_m = cluster_mean_se(list(values), c1).se
    se_d = cluster_mean_se(list(values), c2).se
    se_c = cgm_double_cluster_se(list(values), c1, c2)
    cand = [s for s in (se_m, se_d, se_c) if math.isfinite(s)]
    se = max(cand)
    tag = "match" if se == se_m else ("day" if se == se_d else "cgm")
    print("  %-34s se_match=%.9g se_day=%.9g se_cgm=%.9g -> SE=%.9g (%s) clusters: match=%d day=%d"
          % (label, se_m, se_d, se_c, se, tag,
             cluster_mean_se(list(values), c1).n_clusters, cluster_mean_se(list(values), c2).n_clusters), flush=True)
    return se

print("  -- observed clv --", flush=True)
se_nocross = se_set(clv, wc, wd, "WITHOUT cross-wallet (wallet|match)")
se_cross = se_set(clv, conds, days, "WITH cross-wallet (match)")
ratio = se_cross / se_nocross
print("  SE_with_cross / SE_without = %.6g" % ratio, flush=True)
# means differ because cluster_mean_se averages within cluster first
m_nocross = cluster_mean_se(list(clv), wc).mean
m_cross = cluster_mean_se(list(clv), conds).mean
print("  cluster-weighted mean: without cross=%.9g with cross=%.9g (equal-weight raw=%.9g)"
      % (m_nocross, m_cross, obs_mean), flush=True)

print("  -- shrunk clv --", flush=True)
se_shr_nocross = se_set(shr, wc, wd, "shrunk, WITHOUT cross-wallet")
se_shr_cross = se_set(shr, conds, days, "shrunk, WITH cross-wallet")
print("  SE_with_cross / SE_without (shrunk) = %.6g" % (se_shr_cross / se_shr_nocross), flush=True)

print("", flush=True)
print("=== 4. REQUIRED POOLED n for t=3 and t=2.5 ===", flush=True)
n_obs = len(sel)
# per-pair clustered dispersion, cross-wallet clustering (the honest one)
sd_unit_cross = se_cross * math.sqrt(n_obs)
sd_unit_nocross = se_nocross * math.sqrt(n_obs)
sd_unit_cross_shr = se_shr_cross * math.sqrt(n_obs)
print("  per-pair clustered dispersion: with cross=%.9g | without=%.9g | shrunk with cross=%.9g"
      % (sd_unit_cross, sd_unit_nocross, sd_unit_cross_shr), flush=True)
rows = []
for tlab, T in (("t=3", 3.0), ("t=2.5", 2.5)):
    for elab, edge, sdu in (("observed edge", m_cross, sd_unit_cross),
                            ("shrunk edge", cluster_mean_se(list(shr), conds).mean, sd_unit_cross_shr)):
        need = (T * sdu / edge) ** 2 if edge > 0 else None
        days_need = need / rate if need else None
        add = max(0.0, need - n_obs) if need else None
        rows.append(dict(target=tlab, edge_kind=elab, edge=edge, sd_unit=sdu,
                         n_required=need, days_at_rate=days_need,
                         extra_pairs_beyond_window=add,
                         days_for_extra=(add / rate) if add is not None else None))
        print("  %-6s %-14s edge=%.9g n_required=%.6g -> %.6g days at %.4g pairs/day (window already has %d)"
              % (tlab, elab, edge, need, days_need, rate, n_obs), flush=True)

print("", flush=True)
print("=== 5. WEIGHTING: equal-weight vs size-weighted ===", flush=True)
print("  contracts abs(N): med=%.6g p90=%.6g max=%.6g sum=%.6g" % (np.median(wgt), np.percentile(wgt, 90), wgt.max(), wgt.sum()), flush=True)
# size-weighted cluster SE: aggregate within match using size weights, then between-cluster
def weighted_cluster_se(values, w, keys):
    agg = {}
    for v, ww, k in zip(values, w, keys):
        a = agg.setdefault(k, [0.0, 0.0])
        a[0] += v * ww; a[1] += ww
    per = np.array([a[0] / a[1] for a in agg.values() if a[1] > 0], dtype=float)
    wc_ = np.array([a[1] for a in agg.values() if a[1] > 0], dtype=float)
    m = per.size
    if m < 2:
        return float("inf"), float("nan"), m
    # weighted mean across clusters, weights = cluster size
    mu = float(np.dot(per, wc_) / wc_.sum())
    # variance of weighted mean, cluster-robust
    p = wc_ / wc_.sum()
    var = float(np.sum(p ** 2 * (per - mu) ** 2)) * m / (m - 1)
    return math.sqrt(var), mu, m

se_w_cross, mu_w_cross, m_w = weighted_cluster_se(clv, wgt, conds)
se_w_nocross, mu_w_nocross, m_wn = weighted_cluster_se(clv, wgt, wc)
print("  size-weighted, cross-wallet clusters: mean=%.9g se=%.9g clusters=%d t=%.6g"
      % (mu_w_cross, se_w_cross, m_w, mu_w_cross / se_w_cross), flush=True)
print("  size-weighted, no cross-wallet     : mean=%.9g se=%.9g clusters=%d t=%.6g"
      % (mu_w_nocross, se_w_nocross, m_wn, mu_w_nocross / se_w_nocross), flush=True)
print("  equal-weight,  cross-wallet        : mean=%.9g se=%.9g t=%.6g"
      % (m_cross, se_cross, m_cross / se_cross), flush=True)
concl = "SAME direction (both positive)" if (mu_w_cross > 0) == (m_cross > 0) else "DIVERGENT SIGNS"
print("  conclusion: %s | weighted/equal edge ratio=%.6g" % (concl, mu_w_cross / m_cross), flush=True)
top = np.sort(wgt)[::-1]
print("  concentration: top 1%% of pairs carry %.4g%% of contracts, top 10%% carry %.4g%%"
      % (100.0 * top[:max(1, n_obs // 100)].sum() / wgt.sum(),
         100.0 * top[:max(1, n_obs // 10)].sum() / wgt.sum()), flush=True)
n_req_w = (3.0 * (se_w_cross * math.sqrt(n_obs)) / mu_w_cross) ** 2 if mu_w_cross > 0 else None
print("  size-weighted n_required at t=3 (observed edge): %.6g -> %.6g days" % (n_req_w, n_req_w / rate), flush=True)

out = dict(
    definition=dict(hypothesis="net edge (gross_delta - cost) of the pooled sample of 8 pre-declared wallets is positive",
                    form="single group test, no multiplicity correction",
                    wallets=LIVE, tiers="atp and wta pooled",
                    cross_wallet_clustering="one match shared by two candidates is ONE cluster"),
    pooled=dict(n_pairs=n_obs, distinct_wallets=len({p["wallet"] for p in sel}),
                span_days=SPAN_DAYS, pairs_per_day=rate,
                per_tier={t: sum(1 for p in sel if p["tier"] == t) for t in TIERS},
                per_wallet=byw),
    mean_tier=mean_tier, r_tier=R_TIER,
    edge=dict(equal_weight_observed=obs_mean, equal_weight_shrunk=shr_mean,
              size_weighted_observed=w_obs, size_weighted_shrunk=w_shr,
              cluster_weighted_observed_cross=m_cross, cluster_weighted_observed_nocross=m_nocross),
    overlap=dict(distinct_matches=len(per_match), matches_multi_candidate=len(multi),
                 pairs_in_multi_matches=pairs_in_multi,
                 candidates_per_match_hist={str(k): v for k, v in sorted(occ.items())},
                 distinct_days=len(per_day), days_multi_candidate=sum(1 for ws in per_day.values() if len(ws) > 1)),
    se=dict(observed_nocross=se_nocross, observed_cross=se_cross, ratio=ratio,
            shrunk_nocross=se_shr_nocross, shrunk_cross=se_shr_cross,
            shrunk_ratio=se_shr_cross / se_shr_nocross,
            per_pair_dispersion_cross=sd_unit_cross, per_pair_dispersion_nocross=sd_unit_nocross),
    required_n=rows,
    weighting=dict(size_weighted_mean_cross=mu_w_cross, size_weighted_se_cross=se_w_cross,
                   size_weighted_t=mu_w_cross / se_w_cross, equal_weight_t=m_cross / se_cross,
                   n_required_t3_size_weighted=n_req_w,
                   conclusion=concl,
                   contracts_top1pct_share=float(100.0 * top[:max(1, n_obs // 100)].sum() / wgt.sum()),
                   contracts_top10pct_share=float(100.0 * top[:max(1, n_obs // 10)].sum() / wgt.sum())),
)
json.dump(out, open(HERE / "power_group.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False, allow_nan=True)
print("", flush=True)
print("[out] power_group.json written", flush=True)
