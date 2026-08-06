#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Preregistered outcome order (PREREGISTRATION.md:44-48): UNDECIDABLE first if
# bracket_width > gross_delta, then GO, then NO-GO. bracket_width = phi_S - phi_N
# = feeRate*p*(1-p)^2 per share (POPRAVKA13 2.2). Diagnostic only; the verdict is
# the coordinator's call. ASCII only.
import json, os, sys, math
import numpy as np
from pathlib import Path
HERE = Path(os.path.dirname(os.path.abspath(__file__))); ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
from src.validate.funnel_a import iter_artifact_pairs, day_from_slug
from pm.stats import cluster_mean_se
ART = ROOT / "data" / "collect_window_2026-02-01_2026-04-28.json"
LIVE = ["0xde9f7f4e", "0xa509ae94", "0xdbdd4515", "0x204f72f3", "0x9663a1bc", "0xfbe49f06", "0x70f96881", "0x3d7817cc"]
RATE, SPREAD, FEE = 0.05, 0.005, 0.05
d = json.load(open(ART, encoding="utf-8"))
sel = [p for p in d["pairs"] if any(p["wallet"].startswith(x) for x in LIVE)]
gross = np.array([p["clv"] for p in sel], float)
p = np.array([p["entry_vwap"] for p in sel], float)
days = [day_from_slug(x.get("slug") or "") or x["cond"] for x in sel]
phi_S = FEE * p * (1 - p)          # shares basis, upper
phi_N = FEE * p * p * (1 - p)      # notional basis, lower
bw = phi_S - phi_N                 # = FEE*p*(1-p)^2
g = cluster_mean_se(list(gross), days)
print("=== UNDECIDABLE CHECK (PREREGISTRATION.md:44-48, POPRAVKA13 2.2) ===")
print("  gross_delta (day-cluster mean) = %.9g" % g.mean)
print("  bracket_width per share: mean=%.9g median=%.9g p90=%.9g" % (bw.mean(), np.median(bw), np.percentile(bw, 90)))
bwm = cluster_mean_se(list(bw), days).mean
print("  bracket_width (day-cluster mean) = %.9g" % bwm)
print("  test: bracket_width > gross_delta ? %.9g > %.9g -> %s" % (bwm, g.mean, bwm > g.mean))
print()
print("  net edge under UPPER (shares) fee: %.9g" % cluster_mean_se(list(gross - SPREAD - phi_S), days).mean)
print("  net edge under LOWER (notional) fee: %.9g" % cluster_mean_se(list(gross - SPREAD - phi_N), days).mean)
cs = cluster_mean_se(list(gross - SPREAD - phi_S), days); cn = cluster_mean_se(list(gross - SPREAD - phi_N), days)
print("  t under shares  = %.6g" % (cs.mean / cs.se))
print("  t under notional= %.6g" % (cn.mean / cn.se))
print()
print("  INTERPRETATION: the two fee bases straddle zero -> the sign of net edge is")
print("  not identified by data, it is decided by the unresolved E2 question (fee basis).")
print("  Preregistration calls this outcome UNDECIDABLE and checks it BEFORE GO/NO-GO.")
out = dict(gross_delta_day_cluster=g.mean, bracket_width_day_cluster=bwm,
           bracket_width_gt_gross=bool(bwm > g.mean),
           net_shares=cs.mean, t_shares=cs.mean / cs.se,
           net_notional=cn.mean, t_notional=cn.mean / cn.se,
           straddles_zero=bool(cs.mean <= 0 <= cn.mean),
           note="diagnostic only; formal verdict belongs to the coordinator")
json.dump(out, open(HERE / "power_net_undecidable.json", "w", encoding="utf-8"), indent=1)
print("[out] power_net_undecidable.json")
