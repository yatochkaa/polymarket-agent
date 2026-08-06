#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# capacity.py -- tape-based capacity estimate for the 12 split-half candidates.
#
# NOT order book depth: no historical book snapshots exist for the frozen window
# (/orderbook_history dead since 2026-02-20, live capture started later).
# Everything here is estimated FROM THE TRADE TAPE.
#
# Read-only over data/trades_raw_win/. Writes nothing outside probes/opus/.
#
# Definitions used (fixed here, not invented mid-run):
#  entry window   : per (wallet w, market m, token a) with prematch entries of w,
#                   W = [e_min - 3600, e_max + 3600] intersected with prematch
#                   (ts < gameStartTime), both bounds inclusive. D = 60 minutes.
#  pool           : trades in W on same conditionId + same token a, proxyWallet != w
#                   ("volume traded by OTHER participants", brief item 1).
#  lambda (impact): market property, estimated per (market, token) from ALL prematch
#                   trades of that token ordered by timestamp: OLS through origin of
#                   consecutive price change dP on signed taker volume q.
#                   BUY = +size, SELL = -size (answer Q3, primary).
#                   Sensitivity: |dP| on |q| (unsigned), same estimator.
#                   Fallback to tier-level lambda when a market has < MIN_OBS pairs.
#  capacity       : contracts C such that estimated move = lambda * C = 0.005,
#                   i.e. C = 0.005 / lambda. USD = C * entry VWAP of the candidate.
#  price filter   : raw price outside [0,1] dropped (frozen POPRAVKA 10 rule).
#
# Console output is ASCII only by request.

import json, os, sys, time, math
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
DIR = os.path.join(ROOT, "data", "trades_raw_win")
PASSERS = os.path.join(ROOT, "probes", "deepseek", "splithalf_passers.json")

D_MIN = 60             # half-width of entry window, minutes
MOVE = 0.005           # allowed price move for the headline number
MIN_OBS = 20           # min consecutive-pair observations for a per-market lambda
NAMED = ("0x70f968816adc8dab24e09fd72c9269224cb8f25b",
         "0x204f72f35326db932158cba6adff0b9a1da95e14")


def pctl(a, q):
    a = np.asarray(a, dtype=float)
    return float(np.percentile(a, q)) if a.size else float("nan")


def fit_origin(dp, q):
    """OLS through origin: dp ~ lam * q. Returns (lam, n, r2)."""
    dp = np.asarray(dp, dtype=float)
    q = np.asarray(q, dtype=float)
    ok = np.isfinite(dp) & np.isfinite(q)
    dp, q = dp[ok], q[ok]
    den = float(np.dot(q, q))
    if dp.size < 2 or den <= 0:
        return float("nan"), int(dp.size), float("nan")
    lam = float(np.dot(dp, q) / den)
    ss_tot = float(np.dot(dp, dp))
    resid = dp - lam * q
    ss_res = float(np.dot(resid, resid))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    return lam, int(dp.size), r2


def load_candidates():
    P = json.load(open(PASSERS, encoding="utf-8"))
    tierof = {}
    for t, rows in P["tiers"].items():
        for r in rows:
            tierof.setdefault(r["wallet"], set()).add(t)
    return tierof


def main():
    t0 = time.time()
    tierof = load_candidates()
    W = set(tierof)
    print("[env] python %s numpy %s" % (sys.version.split()[0], np.__version__), flush=True)
    print("[in] trades dir: %s" % DIR, flush=True)
    print("[in] candidates: %d distinct wallets, %d tier-rows" % (len(W), sum(len(v) for v in tierof.values())), flush=True)
    print("[cfg] D=%d min, target move=%.4g, min_obs_per_market=%d, BUY=+size SELL=-size" % (D_MIN, MOVE, MIN_OBS), flush=True)

    mans = [json.loads(l) for l in open(os.path.join(DIR, "manifest.jsonl"), encoding="utf-8")]
    print("[in] manifest rows: %d" % len(mans), flush=True)

    Dsec = D_MIN * 60
    pairs = []            # one record per (wallet, market, token)
    lam_mkt = {}          # (cond, token) -> dict with signed/unsigned lambda
    mkt_vol = {}          # (cond, token) -> total prematch volume (contracts), all participants
    bad_price = 0
    nrow = 0

    for mi, m in enumerate(mans, 1):
        cond, gst, tier = m["cond"], m["gst"], m["tier"]
        with open(os.path.join(DIR, m["file"]), encoding="utf-8") as f:
            tr = json.load(f)
        nrow += len(tr)

        # prematch trades only, valid price, grouped by token
        bytok = {}
        for x in tr:
            p = x.get("price")
            ts = x.get("timestamp")
            if p is None or ts is None:
                continue
            if not (0.0 <= p <= 1.0):
                bad_price += 1
                continue
            if ts >= gst:
                continue
            bytok.setdefault(x["asset"], []).append(x)

        for tok, rows in bytok.items():
            rows.sort(key=lambda r: r["timestamp"])
            price = np.array([r["price"] for r in rows], dtype=float)
            size = np.array([r["size"] for r in rows], dtype=float)
            sgn = np.array([1.0 if r.get("side") == "BUY" else -1.0 for r in rows], dtype=float)
            mkt_vol[(cond, tok)] = float(size.sum())

            # consecutive-trade impact observations
            if price.size >= 2:
                dp = np.diff(price)
                q_signed = (size * sgn)[1:]       # volume of the trade that moved price
                q_abs = size[1:]
                lam_s, n_s, r2_s = fit_origin(dp, q_signed)
                lam_u, n_u, r2_u = fit_origin(np.abs(dp), q_abs)
                lam_mkt[(cond, tok)] = dict(tier=tier, lam_s=lam_s, n=n_s, r2_s=r2_s,
                                            lam_u=lam_u, r2_u=r2_u,
                                            dp=dp, qs=q_signed, qa=q_abs)

            # candidate entries on this token
            for w in W:
                ents = [r for r in rows if r.get("proxyWallet") == w]
                if not ents:
                    continue
                ets = np.array([r["timestamp"] for r in ents], dtype=float)
                esz = np.array([r["size"] for r in ents], dtype=float)
                epr = np.array([r["price"] for r in ents], dtype=float)
                lo, hi = ets.min() - Dsec, ets.max() + Dsec
                inw = (np.array([r["timestamp"] for r in rows], dtype=float) >= lo) & \
                      (np.array([r["timestamp"] for r in rows], dtype=float) <= hi)
                others = [r for r, k in zip(rows, inw) if k and r.get("proxyWallet") != w]
                osz = np.array([r["size"] for r in others], dtype=float) if others else np.zeros(0)
                vwap = float(np.dot(esz, epr) / esz.sum()) if esz.sum() > 0 else float("nan")
                pairs.append(dict(
                    wallet=w, cond=cond, token=tok, tier=tier, slug=m["slug"],
                    title=str(rows[0].get("title", "")).split(":")[0],
                    n_entries=len(ents), entry_size=float(esz.sum()), entry_vwap=vwap,
                    pool_vol=float(osz.sum()), pool_n=int(osz.size),
                    pool_med=pctl(osz, 50), pool_p90=pctl(osz, 90),
                    pool_max=float(osz.max()) if osz.size else float("nan"),
                ))
        if mi % 500 == 0:
            print("  [scan] %d/%d markets, %d pair-records, %.1fs" % (mi, len(mans), len(pairs), time.time() - t0), flush=True)

    print("[scan] done: rows=%d bad_price_dropped=%d pair_records=%d markets_with_lambda=%d %.1fs"
          % (nrow, bad_price, len(pairs), len(lam_mkt), time.time() - t0), flush=True)
    return tierof, pairs, lam_mkt, mkt_vol


def report(tierof, pairs, lam_mkt, mkt_vol):
    out = []
    P = lambda s="": (print(s, flush=True), out.append(s))

    # ---------- tier-level pooled lambda (fallback + reported) ----------
    P("")
    P("=== IMPACT MODEL: lambda per market-token, pooled fallback per tier ===")
    tier_lam = {}
    for tier in ("atp", "wta"):
        dp = np.concatenate([v["dp"] for v in lam_mkt.values() if v["tier"] == tier]) if lam_mkt else np.zeros(0)
        qs = np.concatenate([v["qs"] for v in lam_mkt.values() if v["tier"] == tier]) if lam_mkt else np.zeros(0)
        qa = np.concatenate([v["qa"] for v in lam_mkt.values() if v["tier"] == tier]) if lam_mkt else np.zeros(0)
        ls, ns, r2s = fit_origin(dp, qs)
        lu, nu, r2u = fit_origin(np.abs(dp), qa)
        tier_lam[tier] = dict(lam_s=ls, lam_u=lu)
        P("  tier=%s pooled: n_obs=%d lambda_signed=%.6g (R2=%.4g) lambda_unsigned=%.6g (R2=%.4g)"
          % (tier, ns, ls, r2s, lu, r2u))
        P("      capacity at pooled lambda: signed %.6g contracts | unsigned %.6g contracts"
          % (MOVE / ls if ls > 0 else float("inf"), MOVE / lu if lu > 0 else float("inf")))

    usable = [k for k, v in lam_mkt.items() if v["n"] >= MIN_OBS and np.isfinite(v["lam_s"])]
    P("  market-tokens with own lambda (n_obs>=%d): %d of %d; rest fall back to tier lambda"
      % (MIN_OBS, len(usable), len(lam_mkt)))
    ls_all = np.array([lam_mkt[k]["lam_s"] for k in usable], dtype=float)
    lu_all = np.array([lam_mkt[k]["lam_u"] for k in usable], dtype=float)
    P("  per-market lambda_signed:   med=%.6g p10=%.6g p90=%.6g negative=%d of %d"
      % (np.median(ls_all), pctl(ls_all, 10), pctl(ls_all, 90), int((ls_all <= 0).sum()), ls_all.size))
    P("  per-market lambda_unsigned: med=%.6g p10=%.6g p90=%.6g nonpositive=%d"
      % (np.median(lu_all), pctl(lu_all, 10), pctl(lu_all, 90), int((lu_all <= 0).sum())))

    # ---------- attach capacity to each pair record ----------
    for r in pairs:
        k = (r["cond"], r["token"])
        v = lam_mkt.get(k)
        own = bool(v and v["n"] >= MIN_OBS and np.isfinite(v["lam_s"]))
        r["lam_src"] = "market" if own else "tier"
        ls = v["lam_s"] if own else tier_lam[r["tier"]]["lam_s"]
        lu = v["lam_u"] if own else tier_lam[r["tier"]]["lam_u"]
        r["lam_s"], r["lam_u"] = ls, lu
        r["cap_s"] = (MOVE / ls) if (ls is not None and np.isfinite(ls) and ls > 0) else float("inf")
        r["cap_u"] = (MOVE / lu) if (lu is not None and np.isfinite(lu) and lu > 0) else float("inf")
        r["usd_s"] = r["cap_s"] * r["entry_vwap"] if np.isfinite(r["cap_s"]) else float("inf")
        r["usd_u"] = r["cap_u"] * r["entry_vwap"] if np.isfinite(r["cap_u"]) else float("inf")
        r["mkt_vol"] = mkt_vol.get(k, float("nan"))

    fin_s = np.array([r["cap_s"] for r in pairs], dtype=float)
    P("  pair-records with non-positive signed lambda (capacity undefined/infinite): %d of %d"
      % (int(np.isinf(fin_s).sum()), len(pairs)))

    def block(rows, label):
        if not rows:
            P("  %-26s (no records)" % label)
            return
        pv = np.array([r["pool_vol"] for r in rows], dtype=float)
        pn = np.array([r["pool_n"] for r in rows], dtype=float)
        cs = np.array([r["cap_s"] for r in rows], dtype=float)
        us = np.array([r["usd_s"] for r in rows], dtype=float)
        cu = np.array([r["cap_u"] for r in rows], dtype=float)
        uu = np.array([r["usd_u"] for r in rows], dtype=float)
        csf, usf = cs[np.isfinite(cs)], us[np.isfinite(us)]
        cuf, uuf = cu[np.isfinite(cu)], uu[np.isfinite(uu)]
        P("  %-26s pairs=%5d pool_vol_med=%9.4g pool_n_med=%6.4g | cap_signed med=%9.4g p10=%9.4g USD med=%9.4g p10=%9.4g | cap_unsigned med=%9.4g USD med=%9.4g"
          % (label, len(rows), np.median(pv), np.median(pn),
             np.median(csf) if csf.size else float("nan"), pctl(csf, 10) if csf.size else float("nan"),
             np.median(usf) if usf.size else float("nan"), pctl(usf, 10) if usf.size else float("nan"),
             np.median(cuf) if cuf.size else float("nan"), np.median(uuf) if uuf.size else float("nan")))

    P("")
    P("=== HEADLINE: contracts / USD absorbable before %.4g price move (tape estimate) ===" % MOVE)
    nfb = sum(1 for r in pairs if r["lam_src"] == "tier")
    P("  lambda source: own-market %d, tier-fallback %d (%.4g%% of pairs)"
      % (len(pairs) - nfb, nfb, 100.0 * nfb / len(pairs)))
    block(pairs, "ALL candidate pairs")
    block([r for r in pairs if r["lam_src"] == "market"], "ALL, own-market lambda")
    for tier in ("atp", "wta"):
        block([r for r in pairs if r["tier"] == tier], "tier=" + tier)

    P("")
    P("=== PER WALLET (all tiers pooled) ===")
    for w in sorted({r["wallet"] for r in pairs}, key=lambda x: -len([r for r in pairs if r["wallet"] == x])):
        block([r for r in pairs if r["wallet"] == w], w[:10] + " " + ",".join(sorted(tierof[w])))

    P("")
    P("=== PER WALLET x TIER ===")
    for w in sorted({r["wallet"] for r in pairs}):
        for tier in sorted(tierof[w]):
            block([r for r in pairs if r["wallet"] == w and r["tier"] == tier], w[:10] + " " + tier)
    return out, tier_lam


def report2(tierof, pairs, lam_mkt, mkt_vol, out):
    P = lambda s="": (print(s, flush=True), out.append(s))

    # ---------- decile stratification by market prematch volume (answer Q2 (d)) ----------
    P("")
    P("=== DECILES BY MARKET PREMATCH VOLUME (thin vs thick, data-driven; no tournament classes) ===")
    vols = np.array([v for v in mkt_vol.values()], dtype=float)
    # deciles are pair-weighted: cut on the distribution of the pairs being reported,
    # not on the market-token universe, so each decile holds ~10% of pairs.
    pvols = np.array([r["mkt_vol"] for r in pairs if np.isfinite(r["mkt_vol"])], dtype=float)
    edges = np.percentile(pvols, np.arange(0, 101, 10))
    P("  market-token universe: %d, prematch volume contracts: med=%.6g p10=%.6g p90=%.6g max=%.6g"
      % (vols.size, np.median(vols), pctl(vols, 10), pctl(vols, 90), vols.max()))
    P("  decile edges (contracts): " + " ".join("%.4g" % e for e in edges))
    P("  deciles are pair-weighted (each ~10%% of candidate pairs); fb%% = share using tier-fallback lambda")
    P("  %-6s %-22s %6s %5s %12s %12s %12s %12s" % ("dec", "vol range (contracts)", "pairs", "fb%", "cap_s med", "USD_s med", "cap_u med", "USD_u med"))
    for d in range(10):
        lo, hi = edges[d], edges[d + 1]
        rows = [r for r in pairs if np.isfinite(r["mkt_vol"]) and (r["mkt_vol"] >= lo) and (r["mkt_vol"] <= hi if d == 9 else r["mkt_vol"] < hi)]
        if not rows:
            P("  %-6s %-22s %6d" % ("D%d" % (d + 1), "%.4g..%.4g" % (lo, hi), 0))
            continue
        cs = np.array([r["cap_s"] for r in rows]); us = np.array([r["usd_s"] for r in rows])
        cu = np.array([r["cap_u"] for r in rows]); uu = np.array([r["usd_u"] for r in rows])
        cs, us, cu, uu = cs[np.isfinite(cs)], us[np.isfinite(us)], cu[np.isfinite(cu)], uu[np.isfinite(uu)]
        fb = 100.0 * float(np.mean([r["lam_src"] == "tier" for r in rows]))
        P("  %-6s %-22s %6d %5.1f %12.4g %12.4g %12.4g %12.4g"
          % ("D%d" % (d + 1), "%.4g..%.4g" % (lo, hi), len(rows), fb,
             np.median(cs) if cs.size else float("nan"), np.median(us) if us.size else float("nan"),
             np.median(cu) if cu.size else float("nan"), np.median(uu) if uu.size else float("nan")))

    # ---------- pool size distribution (brief items 1-2) ----------
    P("")
    P("=== POOL IN ENTRY WINDOW (+-%d min, other participants, same token) ===" % D_MIN)
    P("  %-26s %6s %10s %10s %10s %10s %10s" % ("scope", "pairs", "vol_med", "n_med", "sz_med", "sz_p90", "sz_max"))
    def poolrow(rows, label):
        if not rows:
            return
        pv = np.array([r["pool_vol"] for r in rows], dtype=float)
        pn = np.array([r["pool_n"] for r in rows], dtype=float)
        sm = np.array([r["pool_med"] for r in rows], dtype=float)
        s9 = np.array([r["pool_p90"] for r in rows], dtype=float)
        sx = np.array([r["pool_max"] for r in rows], dtype=float)
        f = lambda a: float(np.nanmedian(a)) if np.isfinite(a).any() else float("nan")
        P("  %-26s %6d %10.4g %10.4g %10.4g %10.4g %10.4g" % (label, len(rows), np.median(pv), np.median(pn), f(sm), f(s9), f(sx)))
    poolrow(pairs, "ALL")
    for tier in ("atp", "wta"):
        poolrow([r for r in pairs if r["tier"] == tier], "tier=" + tier)
    for w in sorted({r["wallet"] for r in pairs}):
        poolrow([r for r in pairs if r["wallet"] == w], w[:10])
    P("  pairs with EMPTY pool (no other participant in window): %d of %d"
      % (sum(1 for r in pairs if r["pool_n"] == 0), len(pairs)))

    # ---------- named wallets ----------
    P("")
    P("=== NAMED WALLETS (own executed size = what market DID absorb, NOT capacity) ===")
    for w in NAMED:
        rows = [r for r in pairs if r["wallet"] == w]
        if not rows:
            P("  %s: no pair records" % w[:10]); continue
        esz = np.array([r["entry_size"] for r in rows], dtype=float)
        usd = np.array([r["entry_size"] * r["entry_vwap"] for r in rows], dtype=float)
        cs = np.array([r["cap_s"] for r in rows]); cs = cs[np.isfinite(cs)]
        us = np.array([r["usd_s"] for r in rows]); us = us[np.isfinite(us)]
        cu = np.array([r["cap_u"] for r in rows]); cu = cu[np.isfinite(cu)]
        uu = np.array([r["usd_u"] for r in rows]); uu = uu[np.isfinite(uu)]
        ratio = np.array([r["entry_size"] / r["cap_s"] for r in rows if np.isfinite(r["cap_s"]) and r["cap_s"] > 0], dtype=float)
        P("  %s tiers=%s pairs=%d markets=%d" % (w[:10], ",".join(sorted(tierof[w])), len(rows), len({r["cond"] for r in rows})))
        P("     own entry size per pair (contracts): med=%.4g p90=%.4g max=%.4g" % (np.median(esz), pctl(esz, 90), esz.max()))
        P("     own entry notional per pair (USD)  : med=%.4g p90=%.4g max=%.4g" % (np.median(usd), pctl(usd, 90), usd.max()))
        P("     CAPACITY signed  : contracts med=%.4g p10=%.4g | USD med=%.4g p10=%.4g" % (np.median(cs), pctl(cs, 10), np.median(us), pctl(us, 10)))
        P("     CAPACITY unsigned: contracts med=%.4g p10=%.4g | USD med=%.4g p10=%.4g" % (np.median(cu), pctl(cu, 10), np.median(uu), pctl(uu, 10)))
        P("     own_size / capacity_signed: med=%.4g p90=%.4g share of pairs above 1.0 = %.4g%%"
          % (np.median(ratio), pctl(ratio, 90), 100.0 * float((ratio > 1.0).mean())))

    # ---------- sensitivity comparison ----------
    P("")
    P("=== SENSITIVITY: signed vs unsigned answer to the headline question ===")
    both = [r for r in pairs if np.isfinite(r["cap_s"]) and np.isfinite(r["cap_u"])]
    cs = np.array([r["cap_s"] for r in both]); cu = np.array([r["cap_u"] for r in both])
    rat = cu / cs
    P("  pairs compared: %d" % len(both))
    P("  cap_unsigned / cap_signed: med=%.4g p10=%.4g p90=%.4g" % (np.median(rat), pctl(rat, 10), pctl(rat, 90)))
    for thr in (50, 100, 250, 500):
        P("  share of pairs with capacity >= %4d contracts: signed %.4g%% | unsigned %.4g%%"
          % (thr, 100.0 * float((cs >= thr).mean()), 100.0 * float((cu >= thr).mean())))
    for thr in (50, 100, 250, 500):
        us = np.array([r["usd_s"] for r in both]); uu = np.array([r["usd_u"] for r in both])
        P("  share of pairs with capacity >= $%4d: signed %.4g%% | unsigned %.4g%%"
          % (thr, 100.0 * float((us >= thr).mean()), 100.0 * float((uu >= thr).mean())))
    return out


if __name__ == "__main__":
    tierof, pairs, lam_mkt, mkt_vol = main()
    out, tier_lam = report(tierof, pairs, lam_mkt, mkt_vol)
    out = report2(tierof, pairs, lam_mkt, mkt_vol, out)
    # dump per-pair records for the report tables (own zone only)
    import csv
    csv_path = os.path.join(HERE, "capacity_pairs.csv")
    cols = ["wallet", "tier", "cond", "slug", "title", "token", "n_entries", "entry_size", "entry_vwap",
            "pool_vol", "pool_n", "pool_med", "pool_p90", "pool_max", "mkt_vol",
            "lam_src", "lam_s", "lam_u", "cap_s", "usd_s", "cap_u", "usd_u"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        wc = csv.writer(f)
        wc.writerow(cols)
        for r in pairs:
            wc.writerow([("%.9g" % r[c]) if isinstance(r[c], float) else r[c] for c in cols])
    print("")
    print("[out] per-pair CSV: %s (%d rows)" % (csv_path, len(pairs)), flush=True)
    print("[done]", flush=True)
