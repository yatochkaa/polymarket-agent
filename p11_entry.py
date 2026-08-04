#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# p11_entry.py -- YADRO POPRAVKI 11 (storona vhoda: terminalnoe isklyuchenie iz entry_vwap).
#
# Naznachenie: eto ne samostoyatelnyy progon, a proveryaemoe YADRO, kotoroe vstaet v
# collect_window_v1.py ryadom s :471 (p_ref term-filtr, DOBAVKA 3) i :830 (schetchik term_pre_total).
# Bez seti, bez parquet. Zapusk: python p11_entry.py selftest
#
# ZAMOROZHENO v e857b68 -- NE MENYAT':
#   TERM_HI = 0.999 ; TERM_LO = 0.001
# Predikat terminalnosti VHODA schitaetsya po SYROY cene (kak :830 is_term_price(pr)),
# a NE po tp (tstar).
# VNIMANIE (podtverzhdeno samotestom): predikat NE invarianten k komplementu na rovno 0.999,
# potomu chto 1.0 - 0.999 = 0.0010000000000000009 > TERM_LO. Eto "dyra 0.999", pro kotoruyu
# preduprezhdal John. Sledstvie: raw-schet vhoda mozhet razoytis s tp-schetom offlayn-sreza (18)
# imenno na parah oi=1 s syroy cenoy 0.999 (i simmetrichno). Poetomu gejt drop-to-zero=18 pri
# nesovpadenii NE primiryaet chisla, a OSTANAVLIVAETSYA i dokladyvaet (sm. check_drop_gate).
#
# Pravila POPRAVKI 11 (obyazatelny, soglasovany s Johnom):
#   1) n_trades = len(prematch) DO isklyucheniya -- NE menyaetsya (opredelenie zamorozheno).
#   2) novaya kolonka n_trades_used_p11 = chislo sdelok, realno voshedshih v entry_vwap
#      posle terminalnogo isklyucheniya (storona napravleniya, ne-terminalnye).
#   3) esli n_trades_used_p11 == 0 -> entry_vwap = None i dropped_reason = OTDELNOE novoe
#      znachenie DROP_REASON_P11 (ne pereispolzuem sushchestvuyushchie). Takih par rovno 18
#      (proveryaemoe chislo); ne 18 -> STOP, ne podgonyat.
#   4) napravlenie i p_ref NE trogayutsya (schitayutsya kak v zamorozhennom kode, po VSEM
#      prematch-sdelkam). Menyaetsya tolko chislitel/znamenatel entry (den) i, sledom, clv.
#
# clv (PREREGISTRATION sec.4 "Znak", doslovno):
#   Chistaya dlinnaya poziciya v T*:  CLV = p_ref(T*) - p_vhod.
#   Chistaya korotkaya poziciya:      CLV = p_vhod - p_ref(T*).
#   => clv = direction * (p_ref - entry), direction = +1 (long) / -1 (short).

import sys

# ---- ZAMOROZHENNYE KONSTANTY (e857b68) ----
TERM_HI = 0.999
TERM_LO = 0.001

# Novoe, otdelnoe znachenie dropped_reason (NE pereispolzuet sushchestvuyushchie kody voronki):
DROP_REASON_P11 = "p11_terminal_entry_empty"

# Proveryaemoe chislo par, u kotoryh vhod stal pust posle terminalnogo isklyucheniya:
EXPECTED_DROP_TO_ZERO = 18


def is_term_price(pr):
    """Terminalnost po SYROY cene (semantika :830). Sm. is_term_price v collect_window_v1.py."""
    return pr is not None and (pr >= TERM_HI or pr <= TERM_LO)


def tstar(oi, price):
    """Privedenie k kanonicheskomu tokenu T* (outcomeIndex 0). Kak v zamorozhennom convolve."""
    return price if oi == 0 else (1.0 - price)


def base_dir(oi, side):
    """Napravlenie sdelki v T*. Kak v zamorozhennom convolve."""
    if oi == 0:
        return 1 if side == "BUY" else -1
    return -1 if side == "BUY" else 1


def recompute_pair(prematch, p_ref):
    """Peresчityvaet storonu vhoda dlya odnoy pary i vozvrashchaet i kontrol (A, pravilo OFF),
    i rezultat (B, pravilo ON). prematch: list dict(oi:int{0,1}, price:float[0,1] SYRAYA,
    size:float>0, side:'BUY'|'SELL'). Ceny uzhe proshli filtr bad-prices (POPRAVKA 10).

    Kolonka n_trades ostaetsya len(prematch) -- zdes ne pereopredelyaetsya.
    entry_a  = entry_vwap zamorozhennogo koda (bez isklyucheniya) -> dlya Prohoda A (sverka s stored).
    entry_b  = entry_vwap POPRAVKI 11 (posle terminalnogo isklyucheniya) -> dlya Prohoda B.
    """
    n_trades = len(prematch)
    signed = 0.0
    contribs = []
    for t in prematch:
        oi = t["oi"]; price = t["price"]; size = t["size"]; side = t["side"]
        d = base_dir(oi, side)
        tp = tstar(oi, price)
        term = is_term_price(price)   # SYRAYA cena (semantika :830)
        signed += d * size
        contribs.append((d, size, tp, term))

    if abs(signed) < 1e-9:
        # net-zero: eto sushchestvuyushchiy drop voronki (N=0), NE povod POPRAVKI 11.
        return {
            "n_trades": n_trades, "direction": 0, "net_zero": True,
            "entry_a": None, "den_a": 0.0,
            "entry_b": None, "den_b": 0.0, "n_trades_used_p11": 0,
            "clv_a": None, "clv_b": None, "dropped_p11": False,
            "term_dir_cnt": 0,
        }

    direction = 1 if signed > 0 else -1
    num_a = den_a = 0.0
    num_b = den_b = 0.0
    used = 0            # ne-terminalnye sdelki storony napravleniya (voshli v entry_b)
    term_dir_cnt = 0    # terminalnye sdelki storony napravleniya (iskl. iz entry_b)
    for d, size, tp, term in contribs:
        if d == direction:
            num_a += size * tp
            den_a += size
            if term:
                term_dir_cnt += 1
            else:
                num_b += size * tp
                den_b += size
                used += 1

    entry_a = (num_a / den_a) if den_a > 0 else None
    clv_a = (direction * (p_ref - entry_a)) if (entry_a is not None and p_ref is not None) else None

    if den_b > 0:
        entry_b = num_b / den_b
        dropped_p11 = False
        clv_b = (direction * (p_ref - entry_b)) if p_ref is not None else None
    else:
        entry_b = None
        dropped_p11 = True   # vhod stal pust -> DROP_REASON_P11
        clv_b = None

    return {
        "n_trades": n_trades, "direction": direction, "net_zero": False,
        "entry_a": entry_a, "den_a": den_a,
        "entry_b": entry_b, "den_b": den_b, "n_trades_used_p11": used,
        "clv_a": clv_a, "clv_b": clv_b, "dropped_p11": dropped_p11,
        "term_dir_cnt": term_dir_cnt,
    }


def check_drop_gate(observed, expected=EXPECTED_DROP_TO_ZERO):
    """Gejt Prohoda B: chislo par s pustym vhodom obyazano byt = expected (18). Inache STOP.
    Pri nesovpadenii NE primiryaem (vozmozhna dyra 0.999 raw-vs-tp) -- ostanavlivaemsya i dokladyvaem."""
    if observed != expected:
        raise SystemExit(
            "[FATAL P11] par s pustym vhodom (n_trades_used_p11==0) = %d, ozhidalos' %d -> STOP, ne podgonyaem"
            % (observed, expected)
        )
    return True


def assert_ntrades_sum_invariant(sum_p11, sum_frozen):
    """Priemka Prohoda A: sum(n_trades) v _p11 sovpadaet s ishodnym pairs.parquet pobitovo."""
    if sum_p11 != sum_frozen:
        raise SystemExit(
            "[FATAL P11] sum(n_trades) _p11=%d != frozen=%d -> kolonka n_trades pereopredelena, STOP"
            % (sum_p11, sum_frozen)
        )
    return True


# ============================ SAMOTEST ============================

def _approx(a, b, eps=1e-12):
    return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= eps)


def _selftest():
    print("COMPILE_OK")

    # --- predikat is_term_price po SYROY cene, granicy 0.999/0.001 ---
    assert is_term_price(0.999) and is_term_price(0.001)
    assert not is_term_price(0.9989) and not is_term_price(0.0011)
    assert not is_term_price(0.5) and not is_term_price(None)

    # --- invariantnost k komplementu DERZHITSYA tolko VNE tochnyh granic (float) ---
    for p in (0.9995, 0.0005, 0.5, 0.2, 0.8, 0.9989, 0.0011):
        assert is_term_price(p) == is_term_price(1.0 - p), p

    # --- DYRA na rovno 0.999 (podtverzhdeno): raw-predikat NE invarianten k komplementu ---
    # 1.0 - 0.999 = 0.0010000000000000009 > TERM_LO -> tp NE terminalen, a raw 0.999 -- terminalen.
    assert is_term_price(0.999) is True
    assert is_term_price(1.0 - 0.999) is False     # <- imenno tut raw i tp rashodyatsya
    assert is_term_price(0.001) is True            # raw 0.001 terminalen (semantika :830)

    # --- base_dir / tstar ---
    assert base_dir(0, "BUY") == 1 and base_dir(0, "SELL") == -1
    assert base_dir(1, "BUY") == -1 and base_dir(1, "SELL") == 1
    assert _approx(tstar(0, 0.83), 0.83) and _approx(tstar(1, 0.83), 0.17)

    # === DVA OBYAZATELNYH SAMOTESTA NA GRANICAH pri outcomeIndex=1 ===
    # (1) oi=1, SYRAYA cena rovno 0.999, odna sdelka -> terminalnaya (raw) -> vhod pust -> DROP_REASON_P11.
    r_hi = recompute_pair([{"oi": 1, "price": 0.999, "size": 100.0, "side": "BUY"}], p_ref=0.5)
    assert r_hi["n_trades"] == 1, r_hi                      # n_trades zamorozhen
    assert r_hi["n_trades_used_p11"] == 0 and r_hi["dropped_p11"] is True, r_hi
    assert r_hi["entry_b"] is None and r_hi["clv_b"] is None, r_hi
    assert _approx(r_hi["entry_a"], 1.0 - 0.999), r_hi      # frozen entry = tstar(1,0.999)
    assert r_hi["term_dir_cnt"] == 1, r_hi
    # (2) oi=1, SYRAYA cena rovno 0.001, odna sdelka -> terminalnaya (raw) -> vhod pust -> DROP_REASON_P11.
    r_lo = recompute_pair([{"oi": 1, "price": 0.001, "size": 100.0, "side": "BUY"}], p_ref=0.5)
    assert r_lo["n_trades"] == 1 and r_lo["n_trades_used_p11"] == 0 and r_lo["dropped_p11"] is True, r_lo
    assert _approx(r_lo["entry_a"], 1.0 - 0.001), r_lo      # frozen entry = tstar(1,0.001)

    # granica ne-terminalno pri oi=1: 0.9989 -> ostaetsya vo vhode
    r_edge = recompute_pair([{"oi": 1, "price": 0.9989, "size": 10.0, "side": "BUY"}], p_ref=0.5)
    assert r_edge["n_trades_used_p11"] == 1 and r_edge["dropped_p11"] is False, r_edge
    assert _approx(r_edge["entry_b"], 1.0 - 0.9989), r_edge

    # --- mnogosdelochnaya para: napravlenie ot VSEH sdelok, entry_b bez terminalnyh ---
    pm = [
        {"oi": 0, "price": 0.60,  "size": 100.0, "side": "BUY"},   # dir +1, ne-term, tp=0.60
        {"oi": 0, "price": 0.999, "size": 300.0, "side": "BUY"},   # dir +1, TERM (raw), tp=0.999
        {"oi": 0, "price": 0.40,  "size": 50.0,  "side": "SELL"},  # dir -1 (protiv napravleniya)
    ]
    r = recompute_pair(pm, p_ref=0.70)
    assert r["n_trades"] == 3, r                              # len(prematch), zamorozhen
    assert r["direction"] == 1, r                            # signed = 100+300-50 = +350
    ea = (100*0.60 + 300*0.999) / 400.0                       # frozen entry: dir=+1 sdelki
    assert _approx(r["entry_a"], ea), (r["entry_a"], ea)
    assert _approx(r["entry_b"], 0.60), r                     # terminalnaya 0.999 isklyuchena
    assert r["n_trades_used_p11"] == 1 and r["term_dir_cnt"] == 1 and r["dropped_p11"] is False, r
    assert r["den_a"] == 400.0 and r["den_b"] == 100.0, r    # den menyaetsya (ozhidaemo)
    assert _approx(r["clv_a"], 0.70 - ea) and _approx(r["clv_b"], 0.70 - 0.60), r  # long: p_ref - entry

    # --- clv korotkaya vetv (znak) ---
    rs = recompute_pair([{"oi": 0, "price": 0.30, "size": 20.0, "side": "SELL"}], p_ref=0.45)
    assert rs["direction"] == -1, rs
    assert _approx(rs["entry_b"], 0.30), rs
    assert _approx(rs["clv_b"], 0.30 - 0.45), rs             # short: p_vhod - p_ref

    # --- sum(n_trades) invariantnost: recompute nikogda ne menyaet n_trades ---
    pairs = [pm, [{"oi": 1, "price": 0.001, "size": 5.0, "side": "BUY"}],
             [{"oi": 0, "price": 0.5, "size": 1.0, "side": "BUY"}, {"oi": 0, "price": 0.5, "size": 1.0, "side": "BUY"}]]
    sum_frozen = sum(len(p) for p in pairs)
    sum_p11 = sum(recompute_pair(p, p_ref=0.5)["n_trades"] for p in pairs)
    assert_ntrades_sum_invariant(sum_p11, sum_frozen)
    assert sum_p11 == 6

    # --- gejt drop-to-zero ---
    drops = 0
    for p in ([{"oi": 0, "price": 0.999, "size": 1.0, "side": "BUY"}],
              [{"oi": 1, "price": 0.999, "size": 1.0, "side": "BUY"}],
              [{"oi": 0, "price": 0.001, "size": 1.0, "side": "BUY"}]):
        if recompute_pair(p, p_ref=0.5)["dropped_p11"]:
            drops += 1
    assert drops == 3
    assert check_drop_gate(3, expected=3) is True
    raised = False
    try:
        check_drop_gate(3, expected=18)
    except SystemExit:
        raised = True
    assert raised, "gejt obyazan padat pri nesovpadenii s 18"

    print("SELFTEST OK: is_term_price(RAW,0.999/0.001)+dyra-0.999 + oi=1 granicy(2) + napravlenie/tstar/base_dir"
          " + entry_a==frozen + entry_b(P11) + n_trades zamorozhen + n_trades_used_p11 + drop18-gate"
          " + clv(long/short znak) + sum(n_trades) invariant")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        print("p11_entry.py -- YADRO POPRAVKI 11. Zapusk samotesta: python p11_entry.py selftest")
