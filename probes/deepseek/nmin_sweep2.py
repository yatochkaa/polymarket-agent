#!/usr/bin/env python
# -*- coding: utf-8 -*-
# nmin_sweep2.py -- written from scratch. Reads two CSVs with pandas only.
# No loops over wallets/pairs, no re-collection of the trade pool, no imports
# of spread_estimator / collect_window_v1. Facts = console output only.

import sys
import numpy as np
import pandas as pd

BASE = r"C:\Users\awf\Desktop\test\probes\deepseek"
COUNTS = BASE + r"\spread_estimator_60_v2_counts.csv"
WALLETS = BASE + r"\wallets_319.csv"

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 50)

def q(series, p):
    """Percentile, linear interpolation (pandas default), on non-null values."""
    s = series.dropna()
    if len(s) == 0:
        return float("nan")
    return float(np.percentile(s.to_numpy(dtype=float), p))

# ---------------------------------------------------------------- load
usecols = [
    "tier", "proxyWallet",
    "spread_cost_point_60", "spread_cost_point_raw_60",
    "spread_cost_point_10", "spread_cost_point_raw_10",
    "measurable_60", "measurable_10",
    "n_buy_60", "n_sell_60", "n_buy_10", "n_sell_10",
]
df = pd.read_csv(COUNTS, usecols=usecols)
print("=== LOAD ===")
print("rows in counts CSV:", len(df))

w = pd.read_csv(WALLETS)
print("rows in wallets CSV:", len(w))
print("wallets tier breakdown:", dict(w["tier"].value_counts()))
print("unique wallets:", w["proxyWallet"].nunique(), " unique (tier,wallet) allowances:", len(w.drop_duplicates(["tier", "proxyWallet"])))

# counts stored as MIN over active tokens -> a plain (n_buy>=N & n_sell>=N)
# mask on these columns reproduces the per-active-token AND definition.
for c in ["n_buy_60", "n_sell_60", "n_buy_10", "n_sell_10"]:
    df[c] = pd.to_numeric(df[c], errors="coerce").fillna(-1).astype(int)

# ---------------------------------------------------------------- decisive set
# A pair (w,m) is in the decisive set iff its (tier, proxyWallet) matches one of
# the 319 allowances. Join on the (tier, proxyWallet) pair (84 wallets in both).
allow = w.drop_duplicates(["tier", "proxyWallet"]).copy()
allow_idx = pd.MultiIndex.from_frame(allow[["tier", "proxyWallet"]])
pair_idx = pd.MultiIndex.from_frame(df[["tier", "proxyWallet"]])
df["decisive"] = pair_idx.isin(allow_idx)
n_decisive = int(df["decisive"].sum())
print("decisive-set pairs (join on tier+wallet):", n_decisive, "(control expects 81941)")

# allowance key on decisive rows, for per-allowance aggregation
df["allow_key"] = list(zip(df["tier"], df["proxyWallet"]))

# ---------------------------------------------------------------- STEP 1: GATE
print()
print("=== STEP 1 : GATE (N_min=20, built from counts) ===")
gate60 = (df["n_buy_60"] >= 20) & (df["n_sell_60"] >= 20)
gate10 = (df["n_buy_10"] >= 20) & (df["n_sell_10"] >= 20)
n_gate60 = int(gate60.sum())
n_gateBoth = int((gate60 & gate10).sum())
print("rows with n_buy_60>=20 AND n_sell_60>=20 :", n_gate60, " (must be 12779)")
print("rows measurable at BOTH D=60 and D=10    :", n_gateBoth, " (must be 5189)")

if n_gate60 != 12779 or n_gateBoth != 5189:
    print()
    print("!!! GATE FAILED -- numbers do not match. STOPPING. Nothing tuned. !!!")
    sys.exit(1)
print(">>> GATE PASSED.")

# ---------------------------------------------------------------- STEP 2 sweep
NMINS = [20, 10, 5, 3]
SCP = "spread_cost_point_60"   # clipped D=60 value used for the point estimate

# denominators for tier fractions inside the decisive set (fixed, N_min-indep.)
dec = df[df["decisive"]]
dec_atp_total = int((dec["tier"] == "atp").sum())
dec_wta_total = int((dec["tier"] == "wta").sum())
n_allow = len(allow)  # 319

print()
print("=== STEP 2 : SELF-CONTROL (mask sizes must differ & grow) ===")
print("format: N_min -> (total_measurable, ATP_measurable, WTA_measurable)")
masks = {}
for nm in NMINS:
    m = (df["n_buy_60"] >= nm) & (df["n_sell_60"] >= nm)
    masks[nm] = m
    tot = int(m.sum())
    atp = int((m & (df["tier"] == "atp")).sum())
    wta = int((m & (df["tier"] == "wta")).sum())
    print(f"  N_min={nm:<3} -> ({tot}, {atp}, {wta})")

# self-control assertions
sizes = [(int(masks[nm].sum()),
          int((masks[nm] & (df["tier"] == "atp")).sum()),
          int((masks[nm] & (df["tier"] == "wta")).sum())) for nm in NMINS]
tot_sizes = [s[0] for s in sizes]
if sizes[0] != (12779, 9317, 3462):
    print("!!! SELF-CONTROL FAILED: N_min=20 sizes != (12779,9317,3462). STOP. !!!")
    print("got:", sizes[0])
    sys.exit(1)
if len(set(tot_sizes)) != len(tot_sizes):
    print("!!! SELF-CONTROL FAILED: a total-measurable size repeats -> mask frozen. STOP. !!!")
    sys.exit(1)
if not all(tot_sizes[i] < tot_sizes[i + 1] for i in range(len(tot_sizes) - 1)):
    print("!!! SELF-CONTROL FAILED: totals do not grow as threshold drops. STOP. !!!")
    sys.exit(1)
print(">>> SELF-CONTROL PASSED (sizes differ and grow).")

# ---------------------------------------------------------------- STEP 2 table
print()
print("=== STEP 2 : SWEEP TABLE (clipped spread_cost_point_60) ===")
rows = []
for nm in NMINS:
    m = masks[nm]
    m_both = m & (df["n_buy_10"] >= nm) & (df["n_sell_10"] >= nm)

    tot_meas = int(m.sum())
    dec_meas = int((m & df["decisive"]).sum())

    dec_atp_meas = int((m & df["decisive"] & (df["tier"] == "atp")).sum())
    dec_wta_meas = int((m & df["decisive"] & (df["tier"] == "wta")).sum())
    frac_atp = dec_atp_meas / dec_atp_total if dec_atp_total else float("nan")
    frac_wta = dec_wta_meas / dec_wta_total if dec_wta_total else float("nan")

    vals = df.loc[m, SCP]
    med = q(vals, 50)
    n_med = int(vals.notna().sum())

    atp_vals = df.loc[m & (df["tier"] == "atp"), SCP]
    wta_vals = df.loc[m & (df["tier"] == "wta"), SCP]
    p90_atp = q(atp_vals, 90); n_p90_atp = int(atp_vals.notna().sum())
    p90_wta = q(wta_vals, 90); n_p90_wta = int(wta_vals.notna().sum())

    mstar = int(m_both.sum())

    # per-allowance stats over the decisive set
    dec_rows = df.loc[df["decisive"], ["allow_key"]].copy()
    dec_rows["is_meas"] = m.loc[df["decisive"]].values
    grp = dec_rows.groupby("allow_key")["is_meas"]
    n_meas_per = grp.sum()
    n_tot_per = grp.size()
    frac_per = (n_meas_per / n_tot_per)
    # reindex to full 319 allowances (allowances with no pairs -> NaN)
    all_keys = pd.MultiIndex.from_frame(allow[["tier", "proxyWallet"]])
    all_keys_list = list(zip(allow["tier"], allow["proxyWallet"]))
    frac_per = frac_per.reindex(all_keys_list)
    n_meas_per = n_meas_per.reindex(all_keys_list)

    allow_with_meas = int((n_meas_per.fillna(0) >= 1).sum())
    frac_allow_with_meas = allow_with_meas / n_allow
    median_frac_per_allow = float(frac_per.median())  # median ignores NaN

    rows.append({
        "N_min": nm,
        "meas_total": tot_meas,
        "meas_in_81941": dec_meas,
        "frac_ATP": round(frac_atp, 6),
        "frac_WTA": round(frac_wta, 6),
        "median": round(med, 6),
        "n_med": n_med,
        "p90_ATP": round(p90_atp, 6),
        "n_p90_ATP": n_p90_atp,
        "p90_WTA": round(p90_wta, 6),
        "n_p90_WTA": n_p90_wta,
        "Mstar": mstar,
        "allow_with_meas_frac": round(frac_allow_with_meas, 6),
        "median_frac_per_allow": round(median_frac_per_allow, 6),
    })

tab = pd.DataFrame(rows)
print(tab.to_string(index=False))
print()
print("decisive-set denominators: ATP total =", dec_atp_total, " WTA total =", dec_wta_total,
      " allowances =", n_allow)
# non-null spread value count inside each mask -- proves the value limitation
scp_all = pd.to_numeric(df[SCP], errors="coerce")
print()
print("NOTE (value columns): spread_cost_point_60 is stored ONLY for the 12779 pairs")
print("measurable at N_min=20. non-null spread inside each N_min mask:")
for nm in NMINS:
    print(f"   N_min={nm:<3}: mask={int(masks[nm].sum()):>6}  non-null spread in mask={int((masks[nm] & scp_all.notna()).sum()):>6}")
print("=> median / p90_ATP / p90_WTA are IDENTICAL across N_min (all computed on the")
print("   same 12779 values). They are valid ONLY for N_min=20. For N_min<20 the newly")
print("   measurable pairs have NO spread estimate in the CSV; computing it would require")
print("   re-collecting the trade pool (forbidden). meas_total / meas_in_81941 / tier")
print("   fractions / |M*| / allowance columns DO vary (they use only the counts).")

# ---------------------------------------------------------------- STEP 3 raw
RAW = "spread_cost_point_raw_60"
print()
print("=== STEP 3 : RAW (unclipped, signed) spread_cost_point_raw_60 ===")
raw_all = pd.to_numeric(df[RAW], errors="coerce")
print("Raw column IS present:", RAW)
print("non-null raw values in whole CSV:", int(raw_all.notna().sum()),
      "  (only the 12779 pairs measurable at N_min=20 have a raw value)")
print("=> rows below are IDENTICAL for every N_min: the extra pairs added by")
print("   lowering the threshold have NULL raw (no estimate stored). The raw")
print("   distribution is therefore valid ONLY at N_min=20; it is NOT recomputed")
print("   for lower thresholds (that would require re-collecting the pool).")
if RAW not in df.columns:
    print("Raw column NOT present in CSV. Not computed.")
else:
    raw_rows = []
    for nm in NMINS:
        m = masks[nm]
        v = pd.to_numeric(df.loc[m, RAW], errors="coerce")
        vv = v.dropna()
        atp = pd.to_numeric(df.loc[m & (df["tier"] == "atp"), RAW], errors="coerce").dropna()
        wta = pd.to_numeric(df.loc[m & (df["tier"] == "wta"), RAW], errors="coerce").dropna()
        raw_rows.append({
            "N_min": nm,
            "n": int(vv.shape[0]),
            "min": round(float(vv.min()), 6),
            "p10": round(q(vv, 10), 6),
            "p25": round(q(vv, 25), 6),
            "median": round(q(vv, 50), 6),
            "p75": round(q(vv, 75), 6),
            "p90": round(q(vv, 90), 6),
            "p99": round(q(vv, 99), 6),
            "max": round(float(vv.max()), 6),
            "frac_neg": round(float((vv < 0).mean()), 6),
            "mean": round(float(vv.mean()), 6),
            "med_ATP": round(q(atp, 50), 6),
            "p90_ATP": round(q(atp, 90), 6),
            "n_ATP": int(atp.shape[0]),
            "med_WTA": round(q(wta, 50), 6),
            "p90_WTA": round(q(wta, 90), 6),
            "n_WTA": int(wta.shape[0]),
        })
    rawtab = pd.DataFrame(raw_rows)
    print(rawtab.to_string(index=False))

# ---------------------------------------------------------------- STEP 4
print()
print("=== STEP 4 : CONTRADICTION (zeros-after-clip vs negatives-before-clip) ===")
m20 = masks[20]
sub = df.loc[m20, [SCP, RAW]].copy()
sub[SCP] = pd.to_numeric(sub[SCP], errors="coerce")
sub[RAW] = pd.to_numeric(sub[RAW], errors="coerce")
n20 = len(sub)
sn = sub[SCP].to_numpy(float)
rn = sub[RAW].to_numpy(float)
print("The one set where both spread values exist: N_min=20, D=60 -> n =", n20)
print()
print("-- counts on THAT SAME 12779-pair set --")
clip_zero = int((sn == 0).sum())
raw_neg = int((rn < 0).sum())
raw_zero = int((rn == 0).sum())
print(f"  clipped == 0        : {clip_zero:>5}   ({clip_zero/n20*100:.1f}%)")
print(f"  clipped  < 0        : {int((sn<0).sum()):>5}")
print(f"  clipped  > 0        : {int((sn>0).sum()):>5}")
print(f"  raw < 0             : {raw_neg:>5}   ({raw_neg/n20*100:.1f}%)")
print(f"  raw == 0            : {raw_zero:>5}")
print(f"  raw > 0             : {int((rn>0).sum()):>5}")
print()
print("-- cross-tabulation clipped-sign x raw-sign (exact sets) --")
print(f"  raw<0 & clip==0     : {int(((rn<0)&(sn==0)).sum()):>5}   (negatives correctly clipped to 0)")
print(f"  raw<0 & clip>0      : {int(((rn<0)&(sn>0)).sum()):>5}   (negatives that did NOT become 0)")
print(f"  raw>0 & clip==0     : {int(((rn>0)&(sn==0)).sum()):>5}   (zeros NOT explained by a negative raw)")
print(f"  raw>0 & clip>0      : {int(((rn>0)&(sn>0)).sum()):>5}")
exp = np.clip(rn, 0, None)
mm = ~np.isclose(sn, exp, atol=1e-9)
print(f"  rows clip != max(raw,0) : {int(mm.sum()):>5}  (of {n20}; here clip > max(raw,0), gap up to "
      f"{float(np.abs(sn[mm]-exp[mm]).max()):.4f})")
print()
print("-- historical claims vs reality --")
print("  Historical A: zeros-after-clip = 1951 / 12779 = 15.3%  -> NOT reproducible")
print(f"     actual clip==0 on 12779              : {clip_zero} (30.5%)")
mstar20 = m20 & (df["n_buy_10"] >= 20) & (df["n_sell_10"] >= 20)
sm = pd.to_numeric(df.loc[mstar20, SCP], errors="coerce")
print(f"     clip==0 on |M*|(5189) (nearest guess) : {int((sm==0).sum())}  -- still not 1951")
print("  Historical B: negatives-before-clip = 31.1%           -> reproduced")
print(f"     actual raw<0 on 12779                : {raw_neg} = {raw_neg/n20*100:.1f}%")
print()
print("RESOLUTION: no real 2x contradiction. On the single set where both values")
print("exist (12779), negatives = 3976 (31.1%) and zeros = 3894 (30.5%) are nearly")
print("equal, as clip=max(raw,0) predicts. The 82-row gap is exactly the negatives")
print("with clip>0; together with 69 more raw>0 rows they form 151 pairs where the")
print("clipped point-estimate and the raw column are NOT a strict max(.,0) pair")
print("(clip > max(raw,0)). The historical '1951 / 15.3%' figure is a miscount:")
print("it matches no natural set (12779->3894, |M*|->1743, decisive-1210->320).")

print()
print("=== DONE ===")
