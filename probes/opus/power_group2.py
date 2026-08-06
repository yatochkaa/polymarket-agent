import json, math, os
from pathlib import Path
HERE = Path(os.path.dirname(os.path.abspath(__file__)))
d = json.load(open(HERE / "power_group.json", encoding="utf-8"))
e, s = d["edge"], d["se"]
# t must pair each SE with ITS OWN estimand (cluster_mean_se re-weights by cluster)
t_nc = e["cluster_weighted_observed_nocross"] / s["observed_nocross"]
t_c = e["cluster_weighted_observed_cross"] / s["observed_cross"]
print("t WITHOUT cross-wallet = %.6g / %.6g = %.6g" % (e["cluster_weighted_observed_nocross"], s["observed_nocross"], t_nc))
print("t WITH    cross-wallet = %.6g / %.6g = %.6g" % (e["cluster_weighted_observed_cross"], s["observed_cross"], t_c))
print("t ratio with/without = %.6g  (cost of honest cross-wallet clustering)" % (t_c / t_nc))
print("SE ratio alone = %.6g  -> SE ratio UNDERSTATES the cost; the estimand also moves" % s["ratio"])
n3 = [r for r in d["required_n"] if r["target"] == "t=3"]
print()
print("required pooled n vs what the window already holds (%d pairs):" % d["pooled"]["n_pairs"])
for r in d["required_n"]:
    print("  %-6s %-14s n_req=%9.6g  days=%7.4g  already_sufficient=%s"
          % (r["target"], r["edge_kind"], r["n_required"], r["days_at_rate"],
             r["n_required"] <= d["pooled"]["n_pairs"]))
print()
print("size-weighted t=%.6g vs equal-weight t=%.6g -> ratio %.6g"
      % (d["weighting"]["size_weighted_t"], t_c, d["weighting"]["size_weighted_t"] / t_c))
print("size-weighted n_required(t=3)=%.6g -> %.4g days" % (d["weighting"]["n_required_t3_size_weighted"], d["weighting"]["n_required_t3_size_weighted"] / d["pooled"]["pairs_per_day"]))
d["se"]["t_observed_nocross"] = t_nc
d["se"]["t_observed_cross"] = t_c
d["se"]["t_ratio_cross_over_nocross"] = t_c / t_nc
json.dump(d, open(HERE / "power_group.json", "w", encoding="utf-8"), indent=1, ensure_ascii=False, allow_nan=True)
print("[out] t values appended")
