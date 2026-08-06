#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# power_net.py -- final threshold calc on NET edge (gross_delta - cost).
# cost per share = 0.005 (spread half, P13 4.4a) + 0.05*p*(1-p) (taker fee, shares basis,
# pm/fees.py:154-155 phi_S; P13 2.1 marks shares as the UPPER/conservative branch).
# Reference distribution: Student t with df = n_clusters - 1, plus block bootstrap by day.
# ASCII console only.

import json, os, sys, math
import numpy as np
from pathlib import Path
from collections import defaultdict

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
SPAN_DAYS = 86
RATE = 87.6163
SPREAD = 0.005
FEE_RATE = 0.05
NBOOT = 5000
SEED = 20260806

# ---------- Student t machinery (no scipy) ----------
def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 200, 3.0e-16, 1.0e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    dd = 1.0 - qab * x / qap
    if abs(dd) < FPMIN: dd = FPMIN
    dd = 1.0 / dd
    h = dd
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        dd = 1.0 + aa * dd
        if abs(dd) < FPMIN: dd = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        dd = 1.0 / dd
        h *= dd * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        dd = 1.0 + aa * dd
        if abs(dd) < FPMIN: dd = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        dd = 1.0 / dd
        de = dd * c
        h *= de
        if abs(de - 1.0) < EPS: break
    return h

def betainc(a, b, x):
    if x <= 0.0: return 0.0
    if x >= 1.0: return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - math.exp(lbeta + b * math.log(1.0 - x) + a * math.log(x)) * _betacf(b, a, 1.0 - x) / b

def t_sf(t, df):
    """one-sided upper tail P(T > t) for Student t."""
    x = df / (df + t * t)
    p = 0.5 * betainc(0.5 * df, 0.5, x)
    return p if t > 0 else 1.0 - p

def t_crit(alpha, df):
    """one-sided critical value: P(T > c) = alpha."""
    lo, hi = 0.0, 200.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_sf(mid, df) > alpha: lo = mid
        else: hi = mid
    return 0.5 * (lo + hi)

from statistics import NormalDist
ND = NormalDist()
ALPHA = {"t=3": 1.0 - ND.cdf(3.0), "t=2.5": 1.0 - ND.cdf(2.5)}
print("[env] python %s numpy %s (no scipy: t-quantile implemented locally)" % (sys.version.split()[0], np.__version__), flush=True)
print("[check] t_sf(3, 1e6)=%.8g vs normal %.8g" % (t_sf(3.0, 1_000_000), 1 - ND.cdf(3.0)), flush=True)
print("[cfg] one-sided alpha targets: t=3 -> %.8g | t=2.5 -> %.8g" % (ALPHA["t=3"], ALPHA["t=2.5"]), flush=True)
print("[cfg] cost per share = %.4g + %.4g*p*(1-p)  [shares basis, conservative branch]" % (SPREAD, FEE_RATE), flush=True)

# ---------- data ----------
f1, f5, _ = accumulate_pairs(iter_artifact_pairs(ART))
f1p, _ = filter1_pass(f1)
mean_tier = {}
for t in TIERS:
    ms = [sum(f5[t][w]["clv"]) / len(f5[t][w]["clv"]) for w in sorted(f1p[t]) if f5[t].get(w) and f5[t][w]["clv"]]
    mean_tier[t] = float(np.mean(ms))
print("[mean_tier] atp=%.9g wta=%.9g" % (mean_tier["atp"], mean_tier["wta"]), flush=True)

d = json.load(open(ART, encoding="utf-8"))
sel = [p for p in d["pairs"] if any(p["wallet"].startswith(x) for x in LIVE)]
n_obs = len(sel)
gross = np.array([p["clv"] for p in sel], dtype=float)
price = np.array([p["entry_vwap"] for p in sel], dtype=float)
wgt = np.array([abs(p["N"]) for p in sel], dtype=float)
conds = [p["cond"] for p in sel]
days = [day_from_slug(p.get("slug") or "") or (p.get("cond") or "?") for p in sel]

# cost per share: spread half + taker fee on shares basis (per-share fee = rate*p*(1-p))
fee = FEE_RATE * price * (1.0 - price)
cost = SPREAD + fee
net = gross - cost
net_shr = np.array([mean_tier[p["tier"]] + R_TIER[p["tier"]] * (g - mean_tier[p["tier"]])
                    for p, g in zip(sel, gross)], dtype=float) - cost
print("", flush=True)
print("=== COST COMPONENTS (per share) ===", flush=True)
print("  entry price p: med=%.6g p10=%.6g p90=%.6g" % (np.median(price), np.percentile(price, 10), np.percentile(price, 90)), flush=True)
print("  taker fee: med=%.9g mean=%.9g max=%.9g" % (np.median(fee), fee.mean(), fee.max()), flush=True)
print("  total cost: med=%.9g mean=%.9g (spread %.4g is %.4g%% of median cost)"
      % (np.median(cost), cost.mean(), SPREAD, 100.0 * SPREAD / np.median(cost)), flush=True)
print("  gross med=%.9g -> net med=%.9g" % (np.median(gross), np.median(net)), flush=True)

def cluster_se(values, keys):
    return cluster_mean_se(list(values), keys)

def se_max(values):
    a = cluster_se(values, conds); b = cluster_se(values, days)
    c = cgm_double_cluster_se(list(values), conds, days)
    cand = [s for s in (a.se, b.se, c) if math.isfinite(s)]
    se = max(cand)
    tag = "match" if se == a.se else ("day" if se == b.se else "cgm")
    return se, tag, a, b, c

def wmean_se(values, w, keys):
    agg = {}
    for v, ww, k in zip(values, w, keys):
        x = agg.setdefault(k, [0.0, 0.0]); x[0] += v * ww; x[1] += ww
    per = np.array([a[0] / a[1] for a in agg.values() if a[1] > 0], dtype=float)
    cw = np.array([a[1] for a in agg.values() if a[1] > 0], dtype=float)
    m = per.size
    mu = float(np.dot(per, cw) / cw.sum())
    p = cw / cw.sum()
    var = float(np.sum(p ** 2 * (per - mu) ** 2)) * m / (m - 1)
    return mu, math.sqrt(var), m

print("", flush=True)
print("=== NET EDGE, SE (cross-wallet clustering), t ===", flush=True)
res = {}
for lab, v in (("observed", net), ("shrunk", net_shr)):
    se, tag, a, b, c = se_max(v)
    mu = b.mean if tag == "day" else (a.mean if tag == "match" else float(np.mean(v)))
    mu_day, m_day = b.mean, b.n_clusters
    print("  equal-weight %-8s: mean_day=%.9g se_match=%.9g se_day=%.9g se_cgm=%.9g SE=%.9g (%s) t=%.6g clusters_day=%d"
          % (lab, mu_day, a.se, b.se, c, se, tag, mu_day / se, m_day), flush=True)
    res[("equal", lab)] = dict(mean=mu_day, se=se, tag=tag, t=mu_day / se, m_day=m_day,
                              sd_unit=se * math.sqrt(n_obs))
for lab, v in (("observed", net), ("shrunk", net_shr)):
    mu, se, m = wmean_se(v, wgt, days)
    mu_m, se_m, mm = wmean_se(v, wgt, conds)
    use_se, use_m, tg = (se, m, "day") if se >= se_m else (se_m, mm, "match")
    print("  size-weighted %-8s: mean=%.9g se_day=%.9g se_match=%.9g SE=%.9g (%s) t=%.6g clusters=%d"
          % (lab, mu, se, se_m, use_se, tg, mu / use_se, use_m), flush=True)
    res[("weighted", lab)] = dict(mean=mu, se=use_se, tag=tg, t=mu / use_se, m_day=use_m,
                                  sd_unit=use_se * math.sqrt(n_obs))

print("", flush=True)
print("=== REQUIRED n: normal threshold vs Student t (df = clusters - 1) ===", flush=True)
rows = []
for scheme in ("equal", "weighted"):
    for lab in ("observed", "shrunk"):
        r = res[(scheme, lab)]
        df = r["m_day"] - 1
        for tlab in ("t=3", "t=2.5"):
            T_norm = 3.0 if tlab == "t=3" else 2.5
            T_t = t_crit(ALPHA[tlab], df)
            for crit_lab, T in (("normal", T_norm), ("student_t", T_t)):
                need = (T * r["sd_unit"] / r["mean"]) ** 2 if r["mean"] > 0 else None
                rows.append(dict(scheme=scheme, edge_kind=lab, target=tlab, crit_kind=crit_lab,
                                 crit=T, df=df, edge=r["mean"], sd_unit=r["sd_unit"],
                                 n_required=need, days=(need / RATE) if need else None,
                                 window_sufficient=(need is not None and need <= n_obs)))
        print("  %-8s %-8s df=%d: crit t=3 -> %.6g (normal 3) | crit t=2.5 -> %.6g (normal 2.5)"
              % (scheme, lab, df, t_crit(ALPHA["t=3"], df), t_crit(ALPHA["t=2.5"], df)), flush=True)
print("", flush=True)
print("  %-9s %-9s %-6s %-10s %7s %12s %9s %s" % ("scheme", "edge", "target", "crit_kind", "crit", "n_required", "days", "window_ok"), flush=True)
for r in rows:
    if r["n_required"] is None:
        print("  %-9s %-9s %-6s %-10s %7.4f %12s %9s %s"
              % (r["scheme"], r["edge_kind"], r["target"], r["crit_kind"], r["crit"],
                 "NO n", "never", "net edge <= 0: no sample size reaches positive t"), flush=True)
    else:
        print("  %-9s %-9s %-6s %-10s %7.4f %12.6g %9.4g %s"
              % (r["scheme"], r["edge_kind"], r["target"], r["crit_kind"], r["crit"],
                 r["n_required"], r["days"], r["window_sufficient"]), flush=True)

# ---------- block bootstrap by day ----------
print("", flush=True)
print("=== BLOCK BOOTSTRAP BY DAY (%d reps, seed %d) ===" % (NBOOT, SEED), flush=True)
day_idx = defaultdict(list)
for i, dd in enumerate(days):
    day_idx[dd].append(i)
dkeys = sorted(day_idx)
blocks = [np.array(day_idx[k], dtype=np.int64) for k in dkeys]
M = len(blocks)
print("  day blocks: %d | pairs: %d | block size: med=%.4g min=%d max=%d"
      % (M, n_obs, np.median([len(b) for b in blocks]), min(len(b) for b in blocks), max(len(b) for b in blocks)), flush=True)
rng = np.random.default_rng(SEED)
boot = {}
for scheme in ("equal", "weighted"):
    for lab, v in (("observed", net), ("shrunk", net_shr)):
        stats = np.empty(NBOOT, dtype=float)
        for b in range(NBOOT):
            pick = rng.integers(0, M, size=M)
            idx = np.concatenate([blocks[j] for j in pick])
            if scheme == "equal":
                # day-cluster mean, matching the analytic estimand
                sums = {}
                # vectorised: mean over resampled days of per-day means
                vals = v[idx]
                # rebuild per-day means from the picked blocks
                stats[b] = float(np.mean([v[blocks[j]].mean() for j in pick]))
            else:
                num = 0.0; den = 0.0
                for j in pick:
                    bb = blocks[j]
                    num += float(np.dot(v[bb], wgt[bb])); den += float(wgt[bb].sum())
                stats[b] = num / den
        lo, hi = np.percentile(stats, [2.5, 97.5])
        lo1 = float(np.percentile(stats, 5.0))
        se_b = float(stats.std(ddof=1))
        pt = res[(scheme, lab)]["mean"]
        share_neg = float((stats <= 0).mean())
        boot[(scheme, lab)] = dict(se=se_b, ci_lo=float(lo), ci_hi=float(hi), ci95_1sided_lo=lo1,
                                   mean=float(stats.mean()), share_le_zero=share_neg,
                                   se_analytic=res[(scheme, lab)]["se"],
                                   ratio=se_b / res[(scheme, lab)]["se"],
                                   t_boot=pt / se_b)
        print("  %-9s %-9s point=%.9g boot_mean=%.9g boot_se=%.9g analytic_se=%.9g ratio=%.4f 95%%CI=[%.9g, %.9g] P(edge<=0)=%.4g t_boot=%.4g"
              % (scheme, lab, pt, stats.mean(), se_b, res[(scheme, lab)]["se"], se_b / res[(scheme, lab)]["se"],
                 lo, hi, share_neg, pt / se_b), flush=True)

print("", flush=True)
print("=== BOOTSTRAP vs ANALYTIC: does it diverge? ===", flush=True)
rt = [boot[k]["ratio"] for k in boot]
print("  boot_se / analytic_se: min=%.4f max=%.4f" % (min(rt), max(rt)), flush=True)
diverge = any(r < 0.8 or r > 1.25 for r in rt)
print("  verdict: %s (threshold: any ratio outside 0.80..1.25)" % ("DIVERGES -> bootstrap goes into preregistration" if diverge else "AGREES -> analytic SE is adequate, bootstrap stays as sensitivity"), flush=True)

# required n using bootstrap SE, Student t critical value
print("", flush=True)
print("=== REQUIRED n under bootstrap SE (Student t crit) ===", flush=True)
boot_rows = []
for scheme in ("equal", "weighted"):
    for lab in ("observed", "shrunk"):
        bt = boot[(scheme, lab)]
        r = res[(scheme, lab)]
        df = r["m_day"] - 1
        sd_unit_b = bt["se"] * math.sqrt(n_obs)
        for tlab in ("t=3", "t=2.5"):
            T = t_crit(ALPHA[tlab], df)
            if r["mean"] <= 0:
                boot_rows.append(dict(scheme=scheme, edge_kind=lab, target=tlab, crit=T, df=df,
                                      n_required=None, days=None, window_sufficient=False,
                                      note="net edge <= 0: no n reaches a positive t"))
                print("  %-9s %-9s %-6s crit=%.4f n_required=%12s days=%8s  NET EDGE <= 0"
                      % (scheme, lab, tlab, T, "NO n", "never"), flush=True)
                continue
            need = (T * sd_unit_b / r["mean"]) ** 2
            boot_rows.append(dict(scheme=scheme, edge_kind=lab, target=tlab, crit=T, df=df,
                                  n_required=need, days=need / RATE,
                                  window_sufficient=need <= n_obs))
            print("  %-9s %-9s %-6s crit=%.4f n_required=%12.6g days=%8.4g window_ok=%s"
                  % (scheme, lab, tlab, T, need, need / RATE, need <= n_obs), flush=True)

# ---------- headline line for preregistration ----------
main = [r for r in boot_rows if r["scheme"] == "equal" and r["edge_kind"] == "shrunk"]
m3 = [r for r in main if r["target"] == "t=3"][0]
m25 = [r for r in main if r["target"] == "t=2.5"][0]
HORIZON = 92
print("", flush=True)
print("=== PREREGISTRATION LINE (equal weight, shrunk edge, bootstrap SE, Student t) ===", flush=True)
for lab, r in (("t=3", m3), ("t=2.5", m25)):
    proj = RATE * HORIZON
    if r["n_required"] is None:
        print("  %-6s NO required n exists: net edge is NEGATIVE (%.9g). Sample size cannot repair a negative point estimate."
              % (lab, res[("equal", "shrunk")]["mean"]), flush=True)
    else:
        print("  %-6s required_pairs=%.0f  days=%.1f  projected_by_2026-11-06=%.0f  slack=%.2fx  headroom_days=%.1f"
              % (lab, math.ceil(r["n_required"]), r["days"], proj, proj / r["n_required"], HORIZON - r["days"]), flush=True)

print("", flush=True)
print("=== BREAK-EVEN DIAGNOSTIC ===", flush=True)
gm = cluster_mean_se(list(gross), days)
print("  median cost=%.9g | day-cluster gross mean=%.9g | shortfall=%.9g"
      % (float(np.median(cost)), gm.mean, float(np.median(cost)) - gm.mean), flush=True)
print("  share of pairs with net>0: %.4g%% | with gross>0: %.4g%%"
      % (100.0 * float((net > 0).mean()), 100.0 * float((gross > 0).mean())), flush=True)
fee_n = FEE_RATE * price * price * (1.0 - price)
net_n = gross - (SPREAD + fee_n)
cn = cluster_mean_se(list(net_n), days)
print("  sensitivity, NOTIONAL fee basis phi_N=0.05*p^2*(1-p): median cost=%.9g net mean=%.9g t=%.6g -> %s"
      % (float(np.median(SPREAD + fee_n)), cn.mean, cn.mean / cn.se,
         "still negative" if cn.mean <= 0 else "POSITIVE under notional basis"), flush=True)
print("  sensitivity, spread only (no fee): net mean=%.9g"
      % cluster_mean_se(list(gross - SPREAD), days).mean, flush=True)
print("  sensitivity, fee only (no spread): net mean=%.9g"
      % cluster_mean_se(list(gross - fee), days).mean, flush=True)

out = dict(
    cost_model=dict(spread_half=SPREAD, fee_rate=FEE_RATE,
                    fee_formula="per share phi_S(p) = 0.05*p*(1-p), shares basis (pm/fees.py:154-155); P13 2.1 marks shares as the conservative upper branch",
                    cost_median=float(np.median(cost)), cost_mean=float(cost.mean()),
                    fee_median=float(np.median(fee))),
    pooled=dict(n_pairs=n_obs, pairs_per_day=RATE, span_days=SPAN_DAYS, day_blocks=M),
    net_edge={f"{k[0]}|{k[1]}": dict(mean=v["mean"], se_analytic=v["se"], se_winner=v["tag"],
                                     t=v["t"], clusters=v["m_day"], sd_unit=v["sd_unit"])
              for k, v in res.items()},
    critical_values=dict(alpha_one_sided=ALPHA,
                         note="Student t with df = day clusters - 1 = %d" % (res[("equal", "shrunk")]["m_day"] - 1),
                         t3_student=t_crit(ALPHA["t=3"], res[("equal", "shrunk")]["m_day"] - 1),
                         t25_student=t_crit(ALPHA["t=2.5"], res[("equal", "shrunk")]["m_day"] - 1)),
    required_n_analytic=rows,
    bootstrap=dict(n_reps=NBOOT, seed=SEED, blocks_by="tournament day",
                   results={f"{k[0]}|{k[1]}": v for k, v in boot.items()},
                   diverges=diverge),
    required_n_bootstrap=boot_rows,
    preregistration_line=dict(scheme="equal weight", edge="shrunk", se="block bootstrap by day",
                              reference="Student t, df=%d" % m3["df"],
                              t3=(dict(required_pairs=None, verdict="net edge negative; no n suffices")
                                  if m3["n_required"] is None else
                                  dict(required_pairs=math.ceil(m3["n_required"]), days=m3["days"],
                                       projected_by_target=RATE * HORIZON,
                                       slack_multiple=RATE * HORIZON / m3["n_required"],
                                       headroom_days=HORIZON - m3["days"])),
                              t25=(dict(required_pairs=None, verdict="net edge negative; no n suffices")
                                   if m25["n_required"] is None else
                                   dict(required_pairs=math.ceil(m25["n_required"]), days=m25["days"],
                                        projected_by_target=RATE * HORIZON,
                                        slack_multiple=RATE * HORIZON / m25["n_required"],
                                        headroom_days=HORIZON - m25["days"]))),
)
json.dump(out, open(HERE / "power_net.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False, allow_nan=True)
print("", flush=True)
print("[out] power_net.json written", flush=True)
