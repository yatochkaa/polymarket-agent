#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# probe_p11_gate.py -- read-only razbor raskhozhdeniya geyta POPRAVKI 11 (:936-940).
#
# Fakt: collect_window_v1.py:937 padaet s "par s pustym vhodom (n_trades_used_p11==0, N!=0) = 48,
# ozhidalos' 18". Vyzov: source=raw_win, p11=True.
#
# Podhod: NIKAKOY logiki 'vruchnuyu' NE dupliruem -- povtoryaem podschet chistym vyzovom REALNYH
# funkciy iz collect_window_v1 (read_trades_raw_win, filter_bad_prices, convolve(p11=True), idx_of)
# i p11_entry (is_term_price, tstar/base_dir). Nichego ne pravi, ne kоmmtim, EXPECTED_DROP_TO_ZERO
# NE trogaem.
#
# Output:
#   - tablica "prichina -> kolichestvo" (summa = 48),
#   - otdelnyy otvet po syrym cenam v zone (0.999,1.0] / [0.0,0.001) -- "dyra raw-vs-tp",
#   - probes/deepseek/p11_empty_48.csv s poliami po zadaniyu.

import collections
import csv
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, ROOT)

import collect_window_v1 as cw        # noqa: E402
import p11_entry as p11               # noqa: E402

# --- process-local kesh manifesta (priyom iz run_fast.py): bez nego read_trades_raw_win
#     gonyal by load_repull_manifest (sha256 kazhdogo fajla) na KAZHDYY rynok -> O(N^2),
#     4068^2 sha256 ~ 14.2 TB ~ 2.5-3 ch. S keshem -- odin prohod validacii = 4068 sha256.
#     fajl collect_window_v1.py NE izmenyaetsya; sha256 ne otklyuchaetsya (odnokratno na fajl).
_MANIFEST_CACHE = {}
_orig_load_repull_manifest = cw.load_repull_manifest


def _cached_load_repull_manifest(data_dir, repull_dir=cw.REPULL_DIR_NAME):
    key = (os.path.abspath(data_dir), repull_dir)
    cached = _MANIFEST_CACHE.get(key)
    if cached is None:
        cached = _orig_load_repull_manifest(data_dir, repull_dir)   # real'naya sha-validaciya
        _MANIFEST_CACHE[key] = cached
    return cached


cw.load_repull_manifest = _cached_load_repull_manifest


DATA_DIR = os.path.join(ROOT, "data")
OUT_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p11_empty_48.csv")


def _ts(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_markets():
    """Rekonstrukciya spiska rynkov atp/wta s TOZHE filtraciy, chto collect()/gate:
    manifest status ok, tier v DECISION_TIERS, ne completeness_unreachable (isklyuch. POPRAVKA 8)."""
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


def build_clob(rows):
    """clob takoy, chto idx_of() vernet outcomeIndex (0/1) -- tennis, 2 tokena."""
    tok = {}
    for t in rows:
        oo = t.get("outcomeIndex")
        a = str(t.get("asset") or "")
        if a and oo in (0, 1, "0", "1") and int(oo) not in tok:
            tok[int(oo)] = a
    if 0 not in tok or 1 not in tok:
        return [], set()
    return [tok[0], tok[1]], {tok[0], tok[1]}


def tstar_of(oi, price):
    return price if oi == 0 else (1.0 - price)


def base_dir(oi, side):
    if oi == 0:
        return 1 if side == "BUY" else -1
    return -1 if side == "BUY" else 1


def main():
    markets = load_markets()
    empty = []                      # spisok par s pustym vhodom (== gejt)
    n_pairs_total = 0

    for m in markets:
        cond, gst, slug, tier = m["cond"], m["gst"], m["slug"], m["tier"]
        rows, _ = cw.read_trades_raw_win(DATA_DIR, cond)
        clean, _ = cw.filter_bad_prices(rows, gst, cond, slug)
        clob, clob_set = build_clob(clean)
        by_w = collections.defaultdict(list)
        for t in clean:
            by_w[(t.get("proxyWallet") or "").lower()].append(t)
        for w, wtrades in by_w.items():
            pm = [t for t in wtrades
                  if _ts(t.get("timestamp")) is not None and _ts(t.get("timestamp")) < gst]
            if not pm:
                continue
            N, entry, direction, _two, _used = cw.convolve(pm, clob, clob_set, cond, p11=True)
            n_pairs_total += 1
            if abs(N) >= 1e-9 and entry is None:          # to zhe uslovie, chto v gate :901-908
                trades_info = []
                for t in pm:
                    oi = cw.idx_of(t, clob, clob_set)
                    price = float(t["price"])
                    size = float(t["size"])
                    side = (t.get("side") or "").upper()
                    trades_info.append({
                        "oi": oi, "side": side, "size": size,
                        "raw": price, "tp": tstar_of(oi, price),
                        "bdir": base_dir(oi, side),
                    })
                empty.append({
                    "conditionId": cond, "slug": slug, "proxyWallet": w, "tier": tier,
                    "N": N, "direction": direction, "gameStartTime": gst,
                    "n_prematch": len(pm), "trades": trades_info,
                })

    # ---- klassifikaciya prichin (gruppy po zadaniyu) ----
    groups = collections.Counter()
    zone_any = 0        # syraya cena (0.999,1.0] ili [0.0,0.001), lyubaya prematch sdelka
    zone_dir = 0        # то же, tolko po sdelkam storony napravleniya
    exact_dy = 0        # napravlenie: syraya cena rovno 0.999 ili 0.001 (tochnaya dyra raw-vs-tp)

    for r in empty:
        d_side = [t for t in r["trades"] if t["bdir"] == r["direction"]]
        raw_all = all(p11.is_term_price(t["raw"]) for t in d_side)
        tp_all = all(p11.is_term_price(t["tp"]) for t in d_side)
        if raw_all and tp_all:
            g = "term_by_raw_i_by_Tstar (upala by i pri predikate po tp)"
        elif raw_all and not tp_all:
            g = "term_by_raw_only (dyra 0.999 raw-vs-tp: po tp prosla by)"
        else:
            g = "inaya (napravlenie ne vsyo syroe-terminalno)"
        groups[g] += 1
        r["reason"] = g
        if any((0.999 < t["raw"] <= 1.0) or (0.0 <= t["raw"] < 0.001) for t in r["trades"]):
            zone_any += 1
        if any((0.999 < t["raw"] <= 1.0) or (0.0 <= t["raw"] < 0.001) for t in d_side):
            zone_dir += 1
        if any(t["raw"] in (0.999, 0.001) for t in d_side):
            exact_dy += 1

    # ---- CSV ----
    cols = ["conditionId", "slug", "proxyWallet", "tier", "N", "n_trades_used_p11",
            "gameStartTime", "n_prematch", "raw_prices_dir", "tp_prices_dir", "reason"]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        wcsv = csv.writer(f)
        wcsv.writerow(cols)
        for r in empty:
            d_side = [t for t in r["trades"] if t["bdir"] == r["direction"]]
            raws = ";".join("%.12g" % t["raw"] for t in d_side)
            tps = ";".join("%.12g" % t["tp"] for t in d_side)
            wcsv.writerow([r["conditionId"], r["slug"], r["proxyWallet"], r["tier"],
                           "%.6f" % r["N"], 0, r["gameStartTime"], r["n_prematch"],
                           raws, tps, r["reason"]])

    # ---- vyvod ----
    print("rynkov v vyborke (atp/wta, complete):", len(markets))
    print("par kosheljok x match s prematch:", n_pairs_total)
    print("par s pustym vhodom (== gejt 48):", len(empty))
    print()
    print("TABLICA: prichina -> kolichestvo")
    for g, c in groups.most_common():
        print("  %3d  %s" % (c, g))
    print("  ---")
    print("  SUMMA = %d" % sum(groups.values()))
    print()
    print("Q4. par so syroy cenoy v (0.999,1.0] ili [0.0,0.001):")
    print("   lyubaya prematch sdelka:", zone_any)
    print("   tolko storona napravleniya:", zone_dir)
    print("   tochnaya granica 0.999/0.001 (napravlenie), dyra raw-vs-tp:", exact_dy)
    print("CSV:", OUT_CSV)


if __name__ == "__main__":
    main()
