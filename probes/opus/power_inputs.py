#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# power_inputs.py -- per-candidate per-tier dispersion and edge for forward-test power.
# Read-only over the frozen window artifact. ASCII console output only.

import json, os, math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
ART = os.path.join(ROOT, "data", "collect_window_2026-02-01_2026-04-28.json")
PASSERS = os.path.join(ROOT, "probes", "deepseek", "splithalf_passers.json")
T_TARGET = 3.0
SD_WORKING = 0.1     # the working guess we are checking against reality

print("[in] artifact:", ART, flush=True)
d = json.load(open(ART, encoding="utf-8"))
pairs = d["pairs"]
print("[in] pairs in artifact:", len(pairs), "| report valid_pairs_count:", d["report"]["valid_pairs_count"], flush=True)

P = json.load(open(PASSERS, encoding="utf-8"))
want = []
for tier, rows in P["tiers"].items():
    for r in rows:
        want.append((tier, r["wallet"], r))
print("[in] candidate tier-rows:", len(want), "| distinct wallets:", len({w for _, w, _ in want}), flush=True)

# collect clv per (tier, wallet)
buck = {(t, w): [] for t, w, _ in want}
for p in pairs:
    k = (p["tier"], p["wallet"])
    if k in buck:
        buck[k].append(p["clv"])

rows_out = []
for tier, wallet, src in sorted(want, key=lambda x: (x[0], x[1])):
    v = np.asarray(buck[(tier, wallet)], dtype=float)
    v = v[np.isfinite(v)]
    n = int(v.size)
    mean = float(v.mean()) if n else float("nan")
    # sample sd, ddof=1 (per-pair dispersion, unclustered)
    sd = float(v.std(ddof=1)) if n > 1 else float("nan")
    se = sd / math.sqrt(n) if (n > 1 and math.isfinite(sd)) else float("nan")
    t = mean / se if (math.isfinite(se) and se > 0) else float("nan")
    if math.isfinite(mean) and mean > 0 and math.isfinite(sd):
        n_req = (T_TARGET * sd / mean) ** 2
        n_req_note = None
    else:
        n_req = None
        n_req_note = "mean not positive: n for t=3 not computed"
    rows_out.append(dict(tier=tier, wallet=wallet, n=n, mean_clv=mean, sd_clv=sd, se_mean=se,
                         t=t, n_required_t3=n_req, n_required_note=n_req_note,
                         splithalf_n1=src.get("n1"), splithalf_n2=src.get("n2")))
    print("[calc] %s %s n=%d mean=%.9g sd=%.9g se=%.9g t=%.6g n_req=%s"
          % (tier, wallet[:10], n, mean, sd, se, t,
             ("%.6g" % n_req) if n_req is not None else "n/a"), flush=True)

sds = np.array([r["sd_clv"] for r in rows_out if math.isfinite(r["sd_clv"])], dtype=float)
summ = dict(
    n_rows=len(rows_out),
    sd_median=float(np.median(sds)), sd_min=float(sds.min()), sd_max=float(sds.max()),
    sd_p25=float(np.percentile(sds, 25)), sd_p75=float(np.percentile(sds, 75)),
    sd_mean=float(sds.mean()), sd_iqr=float(np.percentile(sds, 75) - np.percentile(sds, 25)),
    sd_working_guess=SD_WORKING,
    sd_median_over_working=float(np.median(sds) / SD_WORKING),
    rows_above_working=int((sds > SD_WORKING).sum()),
    rows_below_working=int((sds < SD_WORKING).sum()),
)
print("", flush=True)
print("[sd across candidates] median=%.9g min=%.9g p25=%.9g p75=%.9g max=%.9g IQR=%.9g"
      % (summ["sd_median"], summ["sd_min"], summ["sd_p25"], summ["sd_p75"], summ["sd_max"], summ["sd_iqr"]), flush=True)
print("[sd vs working guess %.3g] median/guess=%.6g | rows above=%d below=%d"
      % (SD_WORKING, summ["sd_median_over_working"], summ["rows_above_working"], summ["rows_below_working"]), flush=True)

nreq = np.array([r["n_required_t3"] for r in rows_out if r["n_required_t3"] is not None], dtype=float)
summ["n_required_t3_median"] = float(np.median(nreq))
summ["n_required_t3_min"] = float(nreq.min())
summ["n_required_t3_max"] = float(nreq.max())
summ["rows_with_n_required"] = int(nreq.size)
print("[n for t=3] rows=%d median=%.6g min=%.6g max=%.6g"
      % (nreq.size, summ["n_required_t3_median"], summ["n_required_t3_min"], summ["n_required_t3_max"]), flush=True)

out = dict(
    source=dict(artifact="data/collect_window_2026-02-01_2026-04-28.json",
                pairs_in_artifact=len(pairs),
                candidates="probes/deepseek/splithalf_passers.json",
                window=P["window"]),
    method=dict(sd="sample standard deviation of per-pair clv, ddof=1, unclustered",
                se="sd / sqrt(n)", t="mean / se",
                n_required_t3="(3 * sd / mean)^2, only when mean > 0",
                caveat="unclustered: ignores match/tournament-day clustering, so se is optimistic"),
    summary=summ,
    rows=rows_out,
)
with open(os.path.join(HERE, "power_inputs.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, indent=1, ensure_ascii=False, allow_nan=True)
print("[out] power_inputs.json written", flush=True)
