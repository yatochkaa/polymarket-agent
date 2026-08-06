#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# spread_estimator.py -- POPRAVKA 13, §4.4 spread estimator (read-only, bez seti).
#
# POPRAVKA 2026-08-06 (units_audit): D*60. Timestamps -- yunikc-sekundy.
# Okno zakrytoe: lo(D) <= t <= hi(D).  lo(D)=e_min-D*60, hi(D)=e_max+D*60.
# Pool P(w,m,D) = to zhe conditionId + tot zhe token storony a + proxyWallet != w
#                  + ts < gameStartTime + popadanie v okno (ts v [lo,hi]).
# Strahovka edinic: median(e_min) v [1e9,2e10]; D_sec == D*60. Inache STOP.
# Pered otnosheniem 60/10: n_pool_diff (sravnenie po transactionHash). Esli 0 -> STOP.
# clv BOLShe NE schitaetsya.

import hashlib
import json
import os
import statistics
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)

import collect_window_v1 as cw        # noqa: E402

# --- process-local kesh manifesta (priyom run_fast.py) ---
_MANIFEST_CACHE = {}
_orig_load_repull_manifest = cw.load_repull_manifest


def _cached_load_repull_manifest(data_dir, repull_dir=cw.REPULL_DIR_NAME):
    key = (os.path.abspath(data_dir), repull_dir)
    if key not in _MANIFEST_CACHE:
        _MANIFEST_CACHE[key] = _orig_load_repull_manifest(data_dir, repull_dir)
    return _MANIFEST_CACHE[key]


cw.load_repull_manifest = _cached_load_repull_manifest

DATA_DIR = os.path.join(ROOT, "data")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_MD = os.path.join(HERE, "spread_44_v2.md")
OUT_CSV = os.path.join(HERE, "spread_estimator_60_v2_counts.csv")

D_WORK = 60          # rabochee okno §4.4a
D_ALT = 10           # sravnenie §4.4b
MIN_SIDE = 20        # izmerimost'
SEC_PER_MIN = 60.0


def _ts(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _oi(t):
    oo = t.get("outcomeIndex")
    if oo in (0, 1, "0", "1"):
        return int(oo)
    return None


def load_markets():
    mn = cw.load_repull_manifest(DATA_DIR)
    out = []
    for rec in mn.values():
        if rec.get("tier") not in cw.DECISION_TIERS:
            continue
        if rec.get("completeness_unreachable"):
            continue
        out.append({"cond": rec["cond"], "slug": rec["slug"], "tier": rec["tier"], "gst": rec["gst"]})
    out.sort(key=lambda m: m["cond"])
    return out


def main():
    target = os.path.join(ROOT, "collect_window_v1.py")
    sha = hashlib.sha256(open(target, "rb").read()).hexdigest().upper()
    print("[sha256-start]", sha, flush=True)
    if sha != "FAE5999CC9EEBEAE26087BFEDF449A86DA1156F8C566AE6976C5D2A937543D73":
        print("STOP: SHA256 mismatch", flush=True)
        return
    markets = load_markets()
    print("[spread] rynkov (atp/wta complete):", len(markets), flush=True)

    pairs = []          # po odnomu dlya kazhdoy pary s vhodom
    n_prematch_pairs = 0

    for mi, m in enumerate(markets):
        cond, gst, slug, tier = m["cond"], m["gst"], m["slug"], m["tier"]
        rows, _ = cw.read_trades_raw_win(DATA_DIR, cond)
        clean, _ = cw.filter_bad_prices(rows, gst, cond, slug)

        # predmatch-trades tol'ko (ts < gst) -> array
        ts = []; oi = []; size = []; price = []; is_buy = []; wall = []; txh = []
        wmap = {}
        wall_name = {}
        for t in clean:
            tt = _ts(t.get("timestamp"))
            if tt is None or not (tt < gst):
                continue
            try:
                p = float(t["price"]); s = float(t["size"])
            except Exception:
                continue
            if not (0.0 <= p <= 1.0) or s <= 0.0:
                continue
            o = _oi(t)
            if o is None:
                continue
            side = (t.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue
            w = (t.get("proxyWallet") or "").lower()
            wi = wmap.setdefault(w, len(wmap))
            wall_name[wi] = w
            h = t.get("transactionHash") or t.get("transaction_hash")
            ts.append(tt); oi.append(o); size.append(s); price.append(p)
            is_buy.append(side == "BUY"); wall.append(wi); txh.append(h)

        n = len(ts)
        if n == 0:
            continue
        ts = np.asarray(ts); oi = np.asarray(oi, dtype=np.int8)
        size = np.asarray(size); price = np.asarray(price)
        is_buy = np.asarray(is_buy, dtype=bool); wall = np.asarray(wall, dtype=np.int64)

        wgroups = {}
        for i in range(n):
            wgroups.setdefault(int(wall[i]), []).append(i)

        for wi, idxs in wgroups.items():
            n_prematch_pairs += 1
            ii = np.asarray(idxs)
            o = oi[ii]; b = is_buy[ii]; sz = size[ii]
            # base_dir: oi0 BUY +1 / SELL -1 ; oi1 BUY -1 / SELL +1
            bd = np.where(o == 0, np.where(b, 1.0, -1.0), np.where(b, -1.0, 1.0))
            signed = float((bd * sz).sum())
            if abs(signed) < 1e-9:
                continue                      # net-zero: vhoda net
            direction = 1 if signed > 0 else -1
            emask = (bd == direction)
            eidx = ii[emask]
            e_ts = ts[eidx]; e_oi = oi[eidx]; e_sz = size[eidx]
            sz0 = float(e_sz[e_oi == 0].sum())
            sz1 = float(e_sz[e_oi == 1].sum())
            tokens = {0: sz0, 1: sz1}

            # e_min/e_max PO KAZHDOMU TOKONU vhoda (storona napravleniya)
            emin_a = {}; emax_a = {}
            for a, sa in tokens.items():
                if sa <= 0:
                    continue
                ats = e_ts[e_oi == a]
                emin_a[a] = float(ats.min())
                emax_a[a] = float(ats.max())
            e_min = min(emin_a.values()) if emin_a else None
            e_max = max(emax_a.values()) if emax_a else None

            rec = {"conditionId": cond, "slug": slug, "tier": tier, "proxyWallet": wall_name[wi],
                   "gst": gst, "direction": direction,
                   "e_min": e_min, "e_max": e_max,
                   "sz0": sz0, "sz1": sz1, "two_tokens": (sz0 > 0 and sz1 > 0),
                   "D": {}, "rawD": {}, "meas": {}, "pool_hash": {}}
            for D in (D_WORK, D_ALT):
                pool_hashes = set()
                tstats = {}
                ok = True
                for a, sa in tokens.items():
                    if sa <= 0:
                        continue
                    lo = emin_a[a] - D * SEC_PER_MIN
                    hi = emax_a[a] + D * SEC_PER_MIN
                    msk = (oi == a) & (ts >= lo) & (ts <= hi) & (wall != wi)
                    buy = msk & is_buy
                    sell = msk & (~is_buy)
                    nb = int(buy.sum()); ns = int(sell.sum())
                    vb = float((price * buy * size).sum() / (size * buy).sum()) if nb else None
                    vs = float((price * sell * size).sum() / (size * sell).sum()) if ns else None
                    raw = (vb - vs) if (nb and ns) else None
                    eff = max(raw, 0.0) if raw is not None else None
                    spc = (0.5 * eff) if eff is not None else None
                    raw_spc = (0.5 * raw) if raw is not None else None
                    tstats[a] = {"n_buy": nb, "n_sell": ns, "vwap_buy": vb, "vwap_sell": vs,
                                 "eff": eff, "spc": spc, "raw": raw, "raw_spc": raw_spc}
                    for h in (txh[int(i)] for i in np.flatnonzero(msk)):
                        if h is not None:
                            pool_hashes.add(h)
                    if nb < MIN_SIDE or ns < MIN_SIDE:
                        ok = False
                if ok:
                    num = 0.0; raw_num = 0.0; den = 0.0
                    for a, sa in tokens.items():
                        if sa <= 0:
                            continue
                        num += sa * tstats[a]["spc"]
                        raw_num += sa * tstats[a]["raw_spc"]
                        den += sa
                    rec["D"][D] = num / den
                    rec["rawD"][D] = raw_num / den
                else:
                    rec["D"][D] = None
                    rec["rawD"][D] = None
                rec["meas"][D] = ok
                rec["pool_hash"][D] = pool_hashes
                rec.setdefault("tstats", {})[D] = tstats
            pairs.append(rec)

        if (mi + 1) % 500 == 0:
            print("[spread] %d/%d rynkov | par s vhodom %d" % (mi + 1, len(markets), len(pairs)), flush=True)

    print("[spread] vsego par s predmatch:", n_prematch_pairs, flush=True)
    print("[spread] par s vhodom (N!=0):", len(pairs), flush=True)
    two = sum(1 for p in pairs if p["two_tokens"])
    print("[spread] par s vhodom na oboih tokonakh:", two, flush=True)

    # ---------- STRAHOVKA EDINIC ----------
    es = [p["e_min"] for p in pairs if p["e_min"] is not None]
    med_e = statistics.median(es) if es else None
    d_sec = {D: D * SEC_PER_MIN for D in (D_WORK, D_ALT)}
    print("[units] median(e_min):", med_e, "| ozhidanie v [1e9, 2e10]", flush=True)
    print("[units] D_sec:", d_sec, "| D*60:", {D: D * 60.0 for D in (D_WORK, D_ALT)}, flush=True)
    if med_e is None or not (1e9 <= med_e <= 2e10):
        print("[units] STOP: median(e_min)=%r vne [1e9,2e10]" % med_e, flush=True)
        sys.exit(3)
    for D in (D_WORK, D_ALT):
        if d_sec[D] != D * 60.0:
            print("[units] STOP: D_sec[%d]=%r != D*60" % (D, d_sec[D]), flush=True)
            sys.exit(3)
    print("[units] OK", flush=True)

    # ---------- n_pool_diff / n_band ----------
    n_pool_diff = 0
    n_band = 0
    for p in pairs:
        s10 = p["pool_hash"][D_ALT]
        s60 = p["pool_hash"][D_WORK]
        if s10 != s60:
            n_pool_diff += 1
        if s60 - s10:
            n_band += 1
    print("[pool] n_pool_diff (P10 != P60 po transactionHash):", n_pool_diff, flush=True)
    print("[pool] n_band (est' sdelki, dobavlennye oknom 60 mimo okna 10):", n_band, flush=True)
    if n_pool_diff == 0:
        print("[pool] STOP: n_pool_diff == 0 -> diagnostika, a ne 'drifta net'. "
              "Otpusk, chisla po D=60 odni, otnoshenie schitat' nelzya.", flush=True)
        sys.exit(4)

    # ---------- 1. median spc D=60 ----------
    meas60 = [p for p in pairs if p["meas"][D_WORK]]
    med_a = statistics.median(p["D"][D_WORK] for p in meas60) if meas60 else None
    print("[out1] izmerimyh par pri D=60:", len(meas60), "| median spc:", med_a,
          "| porog 0.025:", ("PROVAL" if (med_a is not None and med_a > 0.025) else "OK"), flush=True)

    # ---------- 3. M*, ratio 60/10 ----------
    mstar = [p for p in pairs if p["meas"][D_WORK] and p["meas"][D_ALT]]
    med60_m = statistics.median(p["D"][D_WORK] for p in mstar) if mstar else None
    med10_m = statistics.median(p["D"][D_ALT] for p in mstar) if mstar else None
    ratio = (med60_m / med10_m) if (med60_m is not None and med10_m) else None
    n_uniq_match = len({p["conditionId"] for p in mstar})
    n_uniq_wallet = len({p["proxyWallet"] for p in mstar})
    share = (len(mstar) / n_prematch_pairs) if n_prematch_pairs else 0.0
    print("[out3] |M*|:", len(mstar), "| med60:", med60_m, "| med10:", med10_m,
          "| ratio:", ratio, "| porog 1.5:",
          ("PROVAL" if (ratio is not None and ratio > 1.5) else "OK"), flush=True)
    print("[out3] dolya |M*| ot par s predmatch-vhodom:", share, "(%d/%d)"
          % (len(mstar), n_prematch_pairs), flush=True)
    print("[out3] razlichnyh matchey:", n_uniq_match, "| razlichnyh koshelkov:", n_uniq_wallet, flush=True)
    if len(mstar) < 200:
        print("[out3] VNIMANIE: |M*| < 200 -> test nemoshchen", flush=True)

    # ---------- 4. neizmerimye pri D=60 po tiram ----------
    unmeas = [p for p in pairs if not p["meas"][D_WORK]]
    by_tier = {}
    for p in unmeas:
        by_tier[p["tier"]] = by_tier.get(p["tier"], 0) + 1
    print("[out4] neizmerimyh pri D=60:", len(unmeas), "| po tiram:", by_tier, flush=True)

    # ---------- CSV (D=60) ----------
    print("[selfcheck] measurable from existing flags D60:", len(meas60), "Mstar:", len(mstar), flush=True)
    if len(meas60) != 12779 or len(mstar) != 5189:
        print("[selfcheck] STOP: no CSV written", flush=True)
        return
    cols = ["conditionId", "slug", "tier", "proxyWallet", "direction", "gst",
            "e_min", "e_max", "sz0", "sz1", "two_tokens",
            "n_buy0", "n_sell0", "vwap_buy0", "vwap_sell0",
            "n_buy1", "n_sell1", "vwap_buy1", "vwap_sell1",
            "eff0", "eff1", "spc0", "spc1",
            "spread_cost_point_60", "measurable_60",
            "spread_cost_point_raw_60",
            "spread_cost_point_10", "measurable_10",
            "spread_cost_point_raw_10",
            "n_buy_60", "n_sell_60", "n_buy_10", "n_sell_10"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        import csv
        wc = csv.writer(f)
        wc.writerow(cols)
        for p in pairs:
            ts60 = p["tstats"][D_WORK]
            t0 = ts60.get(0, {}); t1 = ts60.get(1, {})
            ts10 = p["tstats"][D_ALT]
            active60 = [x for a, sa in ((0, p["sz0"]), (1, p["sz1"])) if sa > 0 for x in (ts60.get(a, {}).get("n_buy", 0), ts60.get(a, {}).get("n_sell", 0))]
            active10 = [x for a, sa in ((0, p["sz0"]), (1, p["sz1"])) if sa > 0 for x in (ts10.get(a, {}).get("n_buy", 0), ts10.get(a, {}).get("n_sell", 0))]
            nb60 = min((ts60.get(a, {}).get("n_buy", 0) for a, sa in ((0, p["sz0"]), (1, p["sz1"])) if sa > 0), default=0)
            ns60 = min((ts60.get(a, {}).get("n_sell", 0) for a, sa in ((0, p["sz0"]), (1, p["sz1"])) if sa > 0), default=0)
            nb10 = min((ts10.get(a, {}).get("n_buy", 0) for a, sa in ((0, p["sz0"]), (1, p["sz1"])) if sa > 0), default=0)
            ns10 = min((ts10.get(a, {}).get("n_sell", 0) for a, sa in ((0, p["sz0"]), (1, p["sz1"])) if sa > 0), default=0)
            wc.writerow([p["conditionId"], p["slug"], p["tier"], p["proxyWallet"], p["direction"],
                         p["gst"], "%.3f" % p["e_min"], "%.3f" % p["e_max"],
                         "%.6g" % p["sz0"], "%.6g" % p["sz1"], int(p["two_tokens"]),
                         t0.get("n_buy", 0), t0.get("n_sell", 0),
                         t0.get("vwap_buy", ""), t0.get("vwap_sell", ""),
                         t1.get("n_buy", 0), t1.get("n_sell", 0),
                         t1.get("vwap_buy", ""), t1.get("vwap_sell", ""),
                         t0.get("eff", ""), t1.get("eff", ""),
                         t0.get("spc", ""), t1.get("spc", ""),
                         ("%.6f" % p["D"][D_WORK]) if p["D"][D_WORK] is not None else "",
                         int(p["meas"][D_WORK]),
                         ("%.6f" % p["rawD"][D_WORK]) if p["rawD"][D_WORK] is not None else "",
                         ("%.6f" % p["D"][D_ALT]) if p["D"][D_ALT] is not None else "",
                         int(p["meas"][D_ALT]),
                         ("%.6f" % p["rawD"][D_ALT]) if p["rawD"][D_ALT] is not None else "",
                         nb60, ns60, nb10, ns10])
    print("[spread] CSV:", OUT_CSV, flush=True)

    # ---------- MD ----------
    lines = []
    lines.append("# ПОПРАВКА 13 — §4.4 v2 spread estimator (probe, read-only, ispravlennye ednicy)")
    lines.append("")
    lines.append("Источник: `data/trades_raw_win` (manifest кеш как run_fast.py), без сети. "
                 "Коллектор/код не тронуты. Единица наблюдения — пара (кошелёк, матч).")
    lines.append("")
    lines.append("Поправка: `D*60` (units_audit 2026-08-06). Окно закрытое, "
                 "lo(D)=e_min-D*60, hi(D)=e_max+D*60, lo<=t<=hi. Таймстемпы — юникс-секунды.")
    lines.append("")
    lines.append("## Страховка единиц")
    lines.append("")
    lines.append("- median(e_min): **%s** (диапазон [1e9, 2e10]: %s)"
                 % (_fmt(med_e), "OK" if (med_e is not None and 1e9 <= med_e <= 2e10) else "FAIL"))
    lines.append("- D_sec == D*60: **%s**" % str(d_sec))
    lines.append("")
    lines.append("## n_pool_diff / n_band")
    lines.append("")
    lines.append("- n_pool_diff (P(w,m,10) != P(w,m,60) по transactionHash): **%d**" % n_pool_diff)
    lines.append("- n_band (сделки, добавляемые окном 60 сверх окна 10): **%d**" % n_band)
    lines.append("- условие отношения 60/10 при n_pool_diff>0: **%s**"
                 % ("выполнено" if n_pool_diff > 0 else "НЕ выполнено (STOP)"))
    lines.append("")
    lines.append("## §4.4a v2 — медиана spread_cost_point, D=60")
    lines.append("")
    lines.append("- медиана по измеримым парам (D=60): **%s**" % _fmt(med_a))
    lines.append("- измеримых пар (D=60): **%d** (всех пар с входом: %d, с предматч-сделками: %d)"
                 % (len(meas60), len(pairs), n_prematch_pairs))
    lines.append("- порог правдоподобия: > 0.025 -> **%s**"
                 % ("ПРОВАЛ" if (med_a is not None and med_a > 0.025) else "OK (<= 0.025)"))
    lines.append("")
    lines.append("## §4.4b v2 — D=10 vs D=60, M* (измеримы в обоих окнах, без импутации)")
    lines.append("")
    lines.append("- |M*| = **%d**; различных матчей: **%d**; различных кошельков: **%d**"
                 % (len(mstar), n_uniq_match, n_uniq_wallet))
    lines.append("- доля |M*| от пар с предматч-входом: **%.4f** (%d/%d)"
                 % (share, len(mstar), n_prematch_pairs))
    lines.append("- median(spread_60 | M*) = **%s**" % _fmt(med60_m))
    lines.append("- median(spread_10 | M*) = **%s**" % _fmt(med10_m))
    lines.append("- отношение 60/10 = **%s**" % _fmt(ratio))
    lines.append("- порог дрейфа: > 1.5 -> **%s**"
                 % ("ПРОВАЛ (дрейф)" if (ratio is not None and ratio > 1.5) else "OK (<= 1.5)"))
    if len(mstar) < 200:
        lines.append("- **ВНИМАНИЕ: |M*| < 200** — тест не мощен.")
    lines.append("")
    lines.append("## Пункт 4 — неизмеримые пары при D=60 (n_buy<20 или n_sell<20)")
    lines.append("")
    lines.append("- итого неизмеримых пар: **%d** из %d пар с входом" % (len(unmeas), len(pairs)))
    for t in ("atp", "wta"):
        lines.append("  - %s: %d" % (t, by_tier.get(t, 0)))
    lines.append("")
    lines.append("## Справочно")
    lines.append("")
    lines.append("- пар с входом на обоих токенах (усреднение с весом по size входов): **%d**" % two)
    lines.append("")
    lines.append("Сырые значения по парам (D=60): `probes/deepseek/spread_estimator_60_v2.csv`.")
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print("[spread] MD:", OUT_MD, flush=True)
    sha = hashlib.sha256(open(target, "rb").read()).hexdigest().upper()
    print("[sha256-end]", sha, flush=True)
    if sha != "FAE5999CC9EEBEAE26087BFEDF449A86DA1156F8C566AE6976C5D2A937543D73":
        print("STOP: SHA256 mismatch at end", flush=True)


def _fmt(v):
    if v is None:
        return "n/a"
    return "%.6f" % v


if __name__ == "__main__":
    main()
