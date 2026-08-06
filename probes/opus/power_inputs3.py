#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# power_inputs3.py -- feasibility of required n by 2026-11-06 at observed tennis rate.
import json, os, sys, math, datetime as dt
import numpy as np
from pathlib import Path
HERE = Path(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open(HERE / "power_inputs_clustered.json", encoding="utf-8"))
rows = {(r["tier"], r["wallet"]): r for r in d["rows"]}

# window span, from the frozen window definition
W0 = dt.date(2026, 2, 1); W1 = dt.date(2026, 4, 28)
span = (W1 - W0).days
TODAY = dt.date(2026, 8, 6); TARGET = dt.date(2026, 11, 6)
horizon = (TARGET - TODAY).days
print("[cfg] window span days=%d | forward horizon %s..%s = %d days" % (span, TODAY, TARGET, horizon), flush=True)

LIVE = ["0xde9f7f4e", "0xa509ae94", "0xdbdd4515", "0x204f72f3", "0x9663a1bc", "0xfbe49f06", "0x70f96881", "0x3d7817cc"]
out = []
print("", flush=True)
print("%-10s %-4s %6s %8s %10s %10s %9s %s" % ("wallet", "tier", "n_win", "pairs/d", "n_both", "proj_pairs", "cover", "verdict"), flush=True)
for pref in LIVE:
    ks = [k for k in rows if k[1].startswith(pref)]
    best = None
    for k in sorted(ks):
        r = rows[k]
        rate = r["n"] / span
        proj = rate * horizon
        need = r["n_both"]
        cover = proj / need
        days_needed = need / rate
        verdict = "reachable" if cover >= 1.0 else "NOT reachable"
        print("%-10s %-4s %6d %8.3f %10.1f %10.1f %9.3f %s (needs %.0f days)"
              % (pref, k[0], r["n"], rate, need, proj, cover, verdict, days_needed), flush=True)
        rec = dict(wallet_prefix=pref, tier=k[0], n_window=r["n"], pairs_per_day=rate,
                   n_required_both=need, projected_pairs_by_target=proj, coverage=cover,
                   days_needed_at_rate=days_needed, reachable_by_target=bool(cover >= 1.0))
        out.append(rec)
        if best is None or cover > best["coverage"]:
            best = rec
    # pooled across tiers for wallets present in both (decision unit is the wallet)
    if len(ks) > 1:
        n_tot = sum(rows[k]["n"] for k in ks)
        rate = n_tot / span
        need = min(rows[k]["n_both"] for k in ks)
        proj = rate * horizon
        print("%-10s %-4s %6d %8.3f %10.1f %10.1f %9.3f pooled-both-tiers (needs %.0f days)"
              % (pref, "all", n_tot, rate, need, proj, proj / need, need / rate), flush=True)
        out.append(dict(wallet_prefix=pref, tier="pooled", n_window=n_tot, pairs_per_day=rate,
                        n_required_both=need, projected_pairs_by_target=proj, coverage=proj / need,
                        days_needed_at_rate=need / rate, reachable_by_target=bool(proj >= need)))

c = np.array([x["coverage"] for x in out if x["tier"] != "pooled"], dtype=float)
nreach = sum(1 for x in out if x["tier"] != "pooled" and x["reachable_by_target"])
print("", flush=True)
print("[summary] tier-rows for live wallets: %d | reachable by %s: %d | not reachable: %d"
      % (c.size, TARGET, nreach, c.size - nreach), flush=True)
print("[summary] coverage (projected/required): median=%.4g min=%.4g max=%.4g" % (np.median(c), c.min(), c.max()), flush=True)
d["feasibility"] = dict(window_span_days=span, today=str(TODAY), target=str(TARGET),
                        horizon_days=horizon,
                        note="rate = n_window/span_days assumed to persist; tennis calendar seasonality NOT modelled",
                        rows=out,
                        reachable_tier_rows=nreach, total_tier_rows=int(c.size),
                        coverage_median=float(np.median(c)))
json.dump(d, open(HERE / "power_inputs_clustered.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False, allow_nan=True)
print("[out] feasibility appended to power_inputs_clustered.json", flush=True)
