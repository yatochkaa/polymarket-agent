#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# maker_branch.py -- maker-side branch: cost = spread half only, zero taker fee.
# Adds SE_cost (P13 3: clustered SE of the SPREAD component; fee is deterministic
# at a fixed basis, and here the fee is zero anyway) so SE = sqrt(SE_edge^2+SE_cost^2).
# Also MEASURES maker fill probability and adverse selection from the trade tape.
# ASCII console output only.

import json, os, sys, math, csv, time
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
SPREAD_CSV = HERE / "spread_estimator_nmin_v2.csv"
TRADES = ROOT / "data" / "trades_raw_win"
R_TIER = {"atp": 0.6474, "wta": 0.6104}
LIVE = ["0xde9f7f4e", "0xa509ae94", "0xdbdd4515", "0x204f72f3",
        "0x9663a1bc", "0xfbe49f06", "0x70f96881", "0x3d7817cc"]
RATE = 87.6163
SPREAD_MED = 0.005          # P13 4.4a median, used only where per-pair value is missing
NBOOT = 5000; SEED = 20260806

# ---- Student t (no scipy) ----
def _betacf(a, b, x):
    MAXIT, EPS, FPMIN = 300, 3e-16, 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0; dd = 1.0 - qab * x / qap
    if abs(dd) < FPMIN: dd = FPMIN
    dd = 1.0 / dd; h = dd
    for m in range(1, MAXIT + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        dd = 1.0 + aa * dd
        if abs(dd) < FPMIN: dd = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        dd = 1.0 / dd; h *= dd * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        dd = 1.0 + aa * dd
        if abs(dd) < FPMIN: dd = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN: c = FPMIN
        dd = 1.0 / dd; de = dd * c; h *= de
        if abs(de - 1.0) < EPS: break
    return h

def betainc(a, b, x):
    if x <= 0: return 0.0
    if x >= 1: return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lb + a * math.log(x) + b * math.log(1 - x)) * _betacf(a, b, x) / a
    return 1.0 - math.exp(lb + b * math.log(1 - x) + a * math.log(x)) * _betacf(b, a, 1 - x) / b

def t_sf(t, df):
    p = 0.5 * betainc(0.5 * df, 0.5, df / (df + t * t))
    return p if t > 0 else 1.0 - p

def t_crit(alpha, df):
    lo, hi = 0.0, 200.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_sf(mid, df) > alpha: lo = mid
        else: hi = mid
    return 0.5 * (lo + hi)

from statistics import NormalDist
ND = NormalDist()
ALPHA = {"t=3": 1 - ND.cdf(3.0), "t=2.5": 1 - ND.cdf(2.5)}
print("[env] python %s numpy %s" % (sys.version.split()[0], np.__version__), flush=True)
print("[cfg] maker branch: cost = spread half ONLY, taker fee = 0 (frozen formula: maker pays no fee)", flush=True)
print("[cfg] SE = sqrt(SE_edge^2 + SE_cost^2), SE_cost = clustered SE of spread component (P13 3)", flush=True)

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

# per-pair spread from the frozen v2 estimator (keyed by conditionId+wallet)
spread_map = {}
with open(SPREAD_CSV, encoding="utf-8") as f:
    for r in csv.DictReader(f):
        v = r["spread_cost_point_60"]
        if v != "":
            spread_map[(r["conditionId"], r["proxyWallet"])] = float(v)
print("[in] per-pair spread values loaded: %d" % len(spread_map), flush=True)
sp = np.array([spread_map.get((p["cond"], p["wallet"]), np.nan) for p in sel], dtype=float)
n_have = int(np.isfinite(sp).sum())
print("[in] pooled pairs=%d | with measured spread=%d (%.4g%%) | imputed with median %.4g=%d"
      % (n_obs, n_have, 100.0 * n_have / n_obs, SPREAD_MED, n_obs - n_have), flush=True)
imputed = ~np.isfinite(sp)
sp_filled = np.where(imputed, SPREAD_MED, sp)
print("[in] spread per pair: med=%.9g mean=%.9g p90=%.9g max=%.9g"
      % (np.median(sp_filled), sp_filled.mean(), np.percentile(sp_filled, 90), sp_filled.max()), flush=True)

gross = np.array([p["clv"] for p in sel], dtype=float)
price = np.array([p["entry_vwap"] for p in sel], dtype=float)
wgt = np.array([abs(p["N"]) for p in sel], dtype=float)
conds = [p["cond"] for p in sel]
days = [day_from_slug(p.get("slug") or "") or p["cond"] for p in sel]
tiers = [p["tier"] for p in sel]

net_obs = gross - sp_filled
gross_shr = np.array([mean_tier[t] + R_TIER[t] * (g - mean_tier[t]) for t, g in zip(tiers, gross)], dtype=float)
net_shr = gross_shr - sp_filled

def se_edge_of(v):
    a = cluster_mean_se(list(v), conds); b = cluster_mean_se(list(v), days)
    c = cgm_double_cluster_se(list(v), conds, days)
    cand = [s for s in (a.se, b.se, c) if math.isfinite(s)]
    se = max(cand)
    tag = "match" if se == a.se else ("day" if se == b.se else "cgm")
    return se, tag, b.mean, b.n_clusters

# SE_cost per P13 3: clustered SE of the spread component (fee is zero here => no fee variance)
se_cost, se_cost_tag, sp_daymean, m_day = se_edge_of(sp_filled)
print("", flush=True)
print("=== SE_cost (P13 3: clustered SE of spread component; fee=0 in maker branch) ===", flush=True)
print("  spread day-cluster mean=%.9g SE_cost=%.9g (%s)" % (sp_daymean, se_cost, se_cost_tag), flush=True)
# P13 1.4: for imputed pairs add the empirical dispersion of the measurable tier distribution
extra = 0.0
for t in TIERS:
    m = np.array([(tt == t) and bool(im) for tt, im in zip(tiers, imputed)], dtype=bool)
    k = int(m.sum())
    if k:
        meas = sp[np.array([(tt == t) and np.isfinite(s) for tt, s in zip(tiers, sp)], dtype=bool)]
        v_t = float(np.var(meas, ddof=1)) if meas.size > 1 else 0.0
        extra += (k ** 2) * v_t / (n_obs ** 2) / max(1, k)
        print("  imputed pairs tier=%s: %d, tier variance of measurable spread=%.9g" % (t, k, v_t), flush=True)
se_cost_total = math.sqrt(se_cost ** 2 + extra)
print("  SE_cost incl. imputation term = %.9g (extra var=%.9g)" % (se_cost_total, extra), flush=True)

def wmean_se(values, w, keys):
    agg = {}
    for v, ww, k in zip(values, w, keys):
        x = agg.setdefault(k, [0.0, 0.0]); x[0] += v * ww; x[1] += ww
    per = np.array([a[0] / a[1] for a in agg.values() if a[1] > 0])
    cw = np.array([a[1] for a in agg.values() if a[1] > 0])
    m = per.size; mu = float(np.dot(per, cw) / cw.sum()); pw = cw / cw.sum()
    var = float(np.sum(pw ** 2 * (per - mu) ** 2)) * m / (m - 1)
    return mu, math.sqrt(var), m

print("", flush=True)
print("=== 1. NET EDGE, MAKER BRANCH (cost = spread only) ===", flush=True)
res = {}
for lab, v in (("observed", net_obs), ("shrunk", net_shr)):
    se_e, tag, mu, m = se_edge_of(v)
    se_tot = math.sqrt(se_e ** 2 + se_cost_total ** 2)
    df = m - 1
    res[("equal", lab)] = dict(mean=mu, se_edge=se_e, se_cost=se_cost_total, se=se_tot,
                               tag=tag, clusters=m, df=df, t=mu / se_tot,
                               t_no_secost=mu / se_e, sd_unit=se_tot * math.sqrt(n_obs))
    print("  equal-weight %-8s: mean=%.9g SE_edge=%.9g SE_cost=%.9g SE_total=%.9g t=%.6g (t without SE_cost=%.6g) df=%d"
          % (lab, mu, se_e, se_cost_total, se_tot, mu / se_tot, mu / se_e, df), flush=True)
for lab, v in (("observed", net_obs), ("shrunk", net_shr)):
    mu_d, se_d, m_d = wmean_se(v, wgt, days)
    mu_m, se_m, m_m = wmean_se(v, wgt, conds)
    se_e, m, tag = (se_d, m_d, "day") if se_d >= se_m else (se_m, m_m, "match")
    mu = mu_d
    se_tot = math.sqrt(se_e ** 2 + se_cost_total ** 2)
    df = len(set(days)) - 1
    res[("weighted", lab)] = dict(mean=mu, se_edge=se_e, se_cost=se_cost_total, se=se_tot,
                                  tag=tag, clusters=m, df=df, t=mu / se_tot,
                                  t_no_secost=mu / se_e, sd_unit=se_tot * math.sqrt(n_obs))
    print("  size-weighted %-8s: mean=%.9g SE_edge=%.9g (%s) SE_total=%.9g t=%.6g (without SE_cost=%.6g) df=%d"
          % (lab, mu, se_e, tag, se_tot, mu / se_tot, mu / se_e, df), flush=True)

print("", flush=True)
print("=== 2. REQUIRED n (Student t, df = days - 1 = %d) ===" % (m_day - 1), flush=True)
req = []
for scheme in ("equal", "weighted"):
    for lab in ("observed", "shrunk"):
        r = res[(scheme, lab)]
        for tlab in ("t=3", "t=2.5"):
            T = t_crit(ALPHA[tlab], r["df"])
            if r["mean"] <= 0:
                req.append(dict(scheme=scheme, edge=lab, target=tlab, crit=T, n=None, days=None))
                print("  %-9s %-9s %-6s crit=%.4f  NO n (edge<=0)" % (scheme, lab, tlab, T), flush=True)
                continue
            need = (T * r["sd_unit"] / r["mean"]) ** 2
            req.append(dict(scheme=scheme, edge=lab, target=tlab, crit=T, n=need, days=need / RATE,
                            window_ok=need <= n_obs))
            print("  %-9s %-9s %-6s crit=%.4f n_required=%10.6g days=%8.4g window_ok=%s"
                  % (scheme, lab, tlab, T, need, need / RATE, need <= n_obs), flush=True)

# ================= 3. MAKER FILL PROBABILITY, MEASURED FROM THE TAPE =========
# Model. A copier sees the candidate's first entry trade at e_min and posts a PASSIVE
# limit at the candidate's entry price, in T* space (P7 3: everything expressed in the
# canonical token, complement trades converted as 1-q with the side flipped).
#   net long T*  (direction=+1) -> maker BUY limit at P: filled when a taker SELLS T* at <= P
#   net short T* (direction=-1) -> maker SELL limit at P: filled when a taker BUYS  T* at >= P
# Two fill rules are reported because queue position is NOT observable:
#   optimistic: a trade AT the price counts as a fill (price <= P / >= P)
#   strict    : only strict price improvement counts (price < P / > P), i.e. the level
#               was actually cleared through, so any queue position would have filled
print("", flush=True)
print("=== 3. MAKER FILL PROBABILITY (measured, not assumed) ===", flush=True)
man = {json.loads(l)["cond"]: json.loads(l) for l in open(TRADES / "manifest.jsonl", encoding="utf-8")}
need_conds = {p["cond"] for p in sel}
print("  markets to re-read from tape: %d" % len(need_conds), flush=True)

# entry times per pair from the raw tape (artifact has no e_min)
WINDOWS = [10, 30, 60, 120]      # minutes; plus "until match start"
t0 = time.time()
tape = {}
for i, c in enumerate(sorted(need_conds), 1):
    m = man.get(c)
    if m is None:
        continue
    rows = json.load(open(TRADES / m["file"], encoding="utf-8"))
    gst = m["gst"]
    ts, pT, buyT = [], [], []
    wal_ts = defaultdict(list)
    for x in rows:
        pr = x.get("price"); t = x.get("timestamp")
        if pr is None or t is None or not (0.0 <= pr <= 1.0) or t >= gst:
            continue
        oi = x.get("outcomeIndex"); sd = x.get("side")
        if oi == 0:
            price_T = pr; taker_buys_T = (sd == "BUY")
        else:
            price_T = 1.0 - pr; taker_buys_T = (sd == "SELL")
        ts.append(t); pT.append(price_T); buyT.append(taker_buys_T)
        wal_ts[x.get("proxyWallet")].append(t)
    o = np.argsort(np.array(ts, dtype=float), kind="stable")
    tape[c] = dict(ts=np.array(ts, dtype=float)[o], p=np.array(pT, dtype=float)[o],
                   buy=np.array(buyT, dtype=bool)[o], gst=gst,
                   wal={w: min(v) for w, v in wal_ts.items()})
    if i % 500 == 0:
        print("    ...%d/%d markets %.1fs" % (i, len(need_conds), time.time() - t0), flush=True)
print("  tape loaded for %d markets in %.1fs" % (len(tape), time.time() - t0), flush=True)

fill = {w: np.zeros(n_obs, dtype=bool) for w in WINDOWS}
fill_strict = {w: np.zeros(n_obs, dtype=bool) for w in WINDOWS}
fill_start = np.zeros(n_obs, dtype=bool)
fill_start_strict = np.zeros(n_obs, dtype=bool)
have_e = np.zeros(n_obs, dtype=bool)
mins_to_start = np.full(n_obs, np.nan)
for i, p in enumerate(sel):
    T = tape.get(p["cond"])
    if T is None: continue
    e = T["wal"].get(p["wallet"])
    if e is None: continue
    have_e[i] = True
    mins_to_start[i] = (T["gst"] - e) / 60.0
    P = p["entry_vwap"]; long_ = (p["direction"] > 0)
    after = T["ts"] > e
    if long_:
        opt = after & (~T["buy"]) & (T["p"] <= P)     # taker sells T* at or below our bid
        strict = after & (~T["buy"]) & (T["p"] < P)
    else:
        opt = after & (T["buy"]) & (T["p"] >= P)      # taker buys T* at or above our ask
        strict = after & (T["buy"]) & (T["p"] > P)
    dt = (T["ts"] - e) / 60.0
    for W in WINDOWS:
        fill[W][i] = bool((opt & (dt <= W)).any())
        fill_strict[W][i] = bool((strict & (dt <= W)).any())
    fill_start[i] = bool(opt.any())
    fill_start_strict[i] = bool(strict.any())
print("  pairs with an identifiable entry time on the tape: %d of %d" % (int(have_e.sum()), n_obs), flush=True)
print("  minutes from entry to match start: med=%.4g p10=%.4g p90=%.4g"
      % (np.nanmedian(mins_to_start), np.nanpercentile(mins_to_start, 10), np.nanpercentile(mins_to_start, 90)), flush=True)

hv = have_e
print("", flush=True)
print("  (a) FILL RATE by window, among %d pairs with known entry time:" % int(hv.sum()), flush=True)
print("      %-22s %10s %10s" % ("window", "optimistic", "strict"), flush=True)
for W in WINDOWS:
    print("      %-22s %9.4g%% %9.4g%%" % ("+%d min" % W, 100.0 * fill[W][hv].mean(), 100.0 * fill_strict[W][hv].mean()), flush=True)
print("      %-22s %9.4g%% %9.4g%%" % ("until match start", 100.0 * fill_start[hv].mean(), 100.0 * fill_start_strict[hv].mean()), flush=True)

W_MAIN = 60
print("", flush=True)
print("  chosen window: +%d min. Rationale: it matches the frozen reference-price horizon" % W_MAIN, flush=True)
print("  (P7 2: p_ref age limit 60 min) and the spread estimator window D_WORK=60 (P13 1.2),", flush=True)
print("  so the fill test uses the same clock the rest of the project is frozen on.", flush=True)

print("", flush=True)
print("  (b) ADVERSE SELECTION: edge on filled vs all pairs (window +%d min, optimistic) ===" % W_MAIN, flush=True)
def blk(mask, label, v):
    if int(mask.sum()) < 2:
        print("      %-28s n=%d (too few)" % (label, int(mask.sum()))); return None
    cm = cluster_mean_se(list(v[mask]), [dd for dd, k in zip(days, mask) if k])
    print("      %-28s n=%5d mean_net=%.9g se_day=%.9g t=%.4g gross_mean=%.9g"
          % (label, int(mask.sum()), cm.mean, cm.se, cm.mean / cm.se if cm.se > 0 else float("nan"),
             float(gross[mask].mean())), flush=True)
    return cm.mean
allm = hv.copy()
a_all = blk(allm, "ALL pairs (known entry)", net_obs)
a_fill = blk(hv & fill[W_MAIN], "FILLED (optimistic)", net_obs)
a_nofill = blk(hv & ~fill[W_MAIN], "NOT filled", net_obs)
a_fs = blk(hv & fill_strict[W_MAIN], "FILLED (strict)", net_obs)
print("", flush=True)
if a_all is not None and a_fill is not None:
    print("      adverse selection delta (filled - all) = %.9g" % (a_fill - a_all), flush=True)
    print("      ratio filled/all = %.6g" % (a_fill / a_all if a_all != 0 else float("nan")), flush=True)
    print("      -> %s" % ("ADVERSE: filled pairs have LOWER edge" if a_fill < a_all else "no adverse selection detected in this measure"), flush=True)

# 4. break-even fill share
print("", flush=True)
print("=== 4. BREAK-EVEN FILL SHARE ===", flush=True)
print("  Reasoning: an unfilled order earns nothing and costs nothing. The realised edge", flush=True)
print("  per ATTEMPT is fill_share * edge_on_filled. That is positive whenever edge_on_filled", flush=True)
print("  is positive, so no fill share makes it negative -- the binding question is POWER:", flush=True)
print("  a lower fill share means fewer effective observations, so required n grows as 1/share.", flush=True)
for scheme, lab in (("equal", "observed"), ("equal", "shrunk")):
    r = res[(scheme, lab)]
    if r["mean"] <= 0:
        print("  %-8s %-8s: edge<=0 already, maker branch does not pay off regardless of fill" % (scheme, lab), flush=True)
        continue
    T = t_crit(ALPHA["t=3"], r["df"])
    need = (T * r["sd_unit"] / r["mean"]) ** 2
    for share in (1.0, float(fill[W_MAIN][hv].mean()), float(fill_strict[W_MAIN][hv].mean())):
        eff_days = need / (RATE * share)
        print("  %-8s %-8s fill=%.4g%% -> attempts needed=%.6g, days=%.4g %s"
              % (scheme, lab, 100 * share, need / share, eff_days,
                 "(within 92d to Nov 6)" if eff_days <= 92 else "(BEYOND Nov 6)"), flush=True)
    crit_share = need / (RATE * 92.0)
    print("  %-8s %-8s: fill share needed to finish by Nov 6 (92 days) = %.4g%%"
          % (scheme, lab, 100.0 * crit_share), flush=True)
# edge on filled must stay above zero: what edge_on_filled kills it
print("", flush=True)
print("  Sharper form: the branch stops paying off when edge on FILLED pairs <= 0.", flush=True)
if a_fill is not None:
    print("  measured edge on filled = %.9g -> %s" % (a_fill, "positive, branch still pays" if a_fill > 0 else "NON-POSITIVE, branch does not pay"), flush=True)
    print("  spread cost that would zero it: %.9g (currently %.9g)" % (a_fill + float(np.median(sp_filled)), float(np.median(sp_filled))), flush=True)

# ---- decisive: shrunk edge ON FILLED pairs, with SE_cost, Student t ----
print("", flush=True)
print("=== DECISIVE: shrunk net edge on FILLED subsample (the copier's actual population) ===", flush=True)
final = {}
for wlab, fmask in (("optimistic", fill[W_MAIN]), ("strict", fill_strict[W_MAIN])):
    mk = hv & fmask
    dsub = [dd for dd, k in zip(days, mk) if k]
    csub = [cc for cc, k in zip(conds, mk) if k]
    nsub = int(mk.sum())
    for lab, v in (("observed", net_obs), ("shrunk", net_shr)):
        a = cluster_mean_se(list(v[mk]), csub); b = cluster_mean_se(list(v[mk]), dsub)
        cg = cgm_double_cluster_se(list(v[mk]), csub, dsub)
        se_e = max(s for s in (a.se, b.se, cg) if math.isfinite(s))
        sc_a = cluster_mean_se(list(sp_filled[mk]), csub); sc_b = cluster_mean_se(list(sp_filled[mk]), dsub)
        sc_c = cgm_double_cluster_se(list(sp_filled[mk]), csub, dsub)
        se_c = max(s for s in (sc_a.se, sc_b.se, sc_c) if math.isfinite(s))
        se_t = math.sqrt(se_e ** 2 + se_c ** 2)
        mu = b.mean
        df = len(set(dsub)) - 1
        t = mu / se_t
        T3 = t_crit(ALPHA["t=3"], df); T25 = t_crit(ALPHA["t=2.5"], df)
        sd_u = se_t * math.sqrt(nsub)
        n3 = (T3 * sd_u / mu) ** 2 if mu > 0 else None
        n25 = (T25 * sd_u / mu) ** 2 if mu > 0 else None
        share = float(fmask[hv].mean())
        final[(wlab, lab)] = dict(n=nsub, mean=mu, se_edge=se_e, se_cost=se_c, se=se_t, t=t, df=df,
                                  n_req_t3=n3, n_req_t25=n25, fill_share=share,
                                  attempts_t3=(n3 / share) if n3 else None,
                                  days_t3=(n3 / share / RATE) if n3 else None,
                                  attempts_t25=(n25 / share) if n25 else None,
                                  days_t25=(n25 / share / RATE) if n25 else None)
        print("  %-11s %-8s n=%5d mean=%.9g SE=%.9g t=%.4g df=%d | n_t3=%s days_t3=%s | n_t25=%s days_t25=%s"
              % (wlab, lab, nsub, mu, se_t, t, df,
                 ("%.6g" % n3) if n3 else "NO n", ("%.4g" % (n3 / share / RATE)) if n3 else "never",
                 ("%.6g" % n25) if n25 else "NO n", ("%.4g" % (n25 / share / RATE)) if n25 else "never"), flush=True)

print("", flush=True)
print("=== BREAK-EVEN: what kills the maker branch ===", flush=True)
ef = final[("optimistic", "shrunk")]["mean"]
eo = final[("optimistic", "observed")]["mean"]
print("  edge on filled, observed = %.9g | shrunk = %.9g" % (eo, ef), flush=True)
print("  spread level that zeroes the SHRUNK filled edge: %.9g (current median spread %.9g)"
      % (ef + float(np.median(sp_filled[hv & fill[W_MAIN]])), float(np.median(sp_filled[hv & fill[W_MAIN]]))), flush=True)
print("  adverse-selection ratio (filled/all): observed %.6g | shrunk %.6g"
      % (eo / res[("equal", "observed")]["mean"], ef / res[("equal", "shrunk")]["mean"] if res[("equal", "shrunk")]["mean"] != 0 else float("nan")), flush=True)
print("  NOTE the correct reading of 'break-even fill share': an unfilled limit order neither", flush=True)
print("  earns nor costs, so a lower fill share does not flip the SIGN of the per-attempt edge;", flush=True)
print("  it scales POWER. The branch dies not from a low fill share but from adverse selection", flush=True)
print("  inside the filled subset, which is already measured above.", flush=True)

out = dict(
    branch="maker: cost = spread half only, taker fee zero (frozen formula: maker pays no fee)",
    se_cost=dict(value=se_cost_total, base=se_cost, imputation_extra_var=extra,
                 rule="P13 3: clustered SE of the spread component; P13 1.4 imputation term added"),
    spread=dict(pairs=n_obs, measured=n_have, imputed=n_obs - n_have,
                median=float(np.median(sp_filled)), mean=float(sp_filled.mean())),
    net_edge={f"{k[0]}|{k[1]}": v for k, v in res.items()},
    required_n=req,
    fill=dict(window_main_min=W_MAIN,
              rationale="60 min matches the frozen p_ref horizon (P7 2) and D_WORK=60 of the spread estimator (P13 1.2)",
              by_window={str(W): dict(optimistic=float(fill[W][hv].mean()), strict=float(fill_strict[W][hv].mean())) for W in WINDOWS},
              until_start=dict(optimistic=float(fill_start[hv].mean()), strict=float(fill_start_strict[hv].mean())),
              pairs_with_entry_time=int(hv.sum()),
              minutes_to_start_median=float(np.nanmedian(mins_to_start))),
    adverse_selection=dict(all_pairs_edge=a_all, filled_edge=a_fill, notfilled_edge=a_nofill,
                           filled_strict_edge=a_fs, delta=a_fill - a_all, ratio=a_fill / a_all),
    decisive={f"{k[0]}|{k[1]}": v for k, v in final.items()},
)
json.dump(out, open(HERE / "maker_branch.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False, allow_nan=True)
print("", flush=True)
print("[out] maker_branch.json written", flush=True)
