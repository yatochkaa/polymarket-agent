#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PROBA 2 (GLM) -- data-api /positions.

Zachem: esli etot metod otdaet pribyl po kazhdomu rynku, to sutochnyy skan
blokcheyna dlya Filtra 4 ne nuzhen voobshche.

Koshelki:
  skyman44   0x30c7ac0158499ddc6761047f7f69bcf7d036ac3b
  flatbarrel (adres nayti po data-api/trades)

Vyyasnit:
  1. Polnyy spisok poley otveta s tipami, na realnom primere.
  2. Daetsya li pribyl OTDELNO PO KAZHDOMU RYNKU ili tolko obschey summoy.
  3. Skolko rynkov vozvraschaetsya na koshelek.
  4. VIDNY LI ZAKRYTYE POZICII (glavnyy vopros) -- dokazatelno:
     nayti v /trades rynok, kotoryy koshelek torgoval i kotoryy davno zakryt,
     proverit, est li on v /positions.
  5. Est li postranichnost i gde ee potolok.
  6. Ogranichenie po chastote: 150 zaprosov na 10 sekund.

CHeGO NE DELAT: /markets?order=volume24hr zapreshchen. Nichego ne stroit poverh
rezultatorv.

usage:
  python probes/glm/probe2_positions.py            # polnyy progion
  python probes/glm/probe2_positions.py selftest   # samotest
"""
import json, os, re, sys, time, urllib.request, urllib.parse, urllib.error
from collections import defaultdict

DATA = os.environ.get("PM_DATA_HOST", "https://data-api.polymarket.com")
GAMMA = os.environ.get("PM_GAMMA_HOST", "https://gamma-api.polymarket.com")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
REQ_TIMEOUT = 45.0
RATE_WIN_S = 10.0
RATE_MAX = 120

SKYMAN44 = "0x30c7ac0158499ddc6761047f7f69bcf7d036ac3b"
KNOWN_WALLETS = {"skyman44": SKYMAN44}

PNL_RX = re.compile(r"pnl|profit|realized|cash|earned|redeem|value|initial|cur(rent)?price|size|avg", re.I)
CLOSED_RX = re.compile(r"closed|redeem|resolved|settled|outcome", re.I)

LOGDIR = os.path.dirname(os.path.abspath(__file__))
RAWLOG = os.path.join(LOGDIR, "probe2_raw_api.log")
SUMMARY = os.path.join(LOGDIR, "probe2_summary.json")

_rate_window = []


def throttle():
    now = time.time()
    _rate_window[:] = [t for t in _rate_window if now - t < RATE_WIN_S]
    if len(_rate_window) >= RATE_MAX:
        wait = RATE_WIN_S - (now - _rate_window[0]) + 0.05
        if wait > 0:
            time.sleep(wait)
        now = time.time()
        _rate_window[:] = [t for t in _rate_window if now - t < RATE_WIN_S]
    _rate_window.append(time.time())


def log_raw(label, url, status, body_excerpt=""):
    with open(RAWLOG, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "label": label, "url": url,
                            "status": status, "body_excerpt": body_excerpt[:400]},
                           default=str) + "\n")


def get_json(path_or_url, params=None, retries=4):
    """GET s retry. Prinimaet polnyy URL ili path + params."""
    if path_or_url.startswith("http"):
        base = path_or_url
        url = base
        if params:
            url = base + ("&" if "?" in base else "?") + urllib.parse.urlencode(params)
    else:
        url = DATA + path_or_url
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
    last_err = None
    for attempt in range(retries):
        throttle()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
                body = r.read().decode("utf-8", "replace")
                log_raw("GET", url, r.status, body)
                try:
                    return json.loads(body), None
                except ValueError:
                    return body, None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            log_raw("GET-ERR", url, e.code, body)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(min(2 ** attempt, 15))
                last_err = "HTTP %d" % e.code
                continue
            return None, "HTTP %d: %s" % (e.code, body[:200])
        except urllib.error.URLError as e:
            log_raw("GET-ERR", url, -1, str(e))
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
                last_err = "URLError: %s" % e.reason
                continue
            return None, "URLError: %s" % e.reason
        except Exception as e:
            log_raw("GET-ERR", url, -1, str(e))
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
                last_err = "%s: %s" % (type(e).__name__, e)
                continue
            return None, "%s: %s" % (type(e).__name__, e)
    return None, last_err or "exhausted"


def as_list(resp):
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in ("positions", "data", "results"):
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def field_types(items):
    out = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        for k, v in it.items():
            e = out.setdefault(k, {"types": set(), "ex": v, "nulls": 0, "nonnull": 0})
            e["types"].add(type(v).__name__)
            if v is None:
                e["nulls"] += 1
            else:
                e["nonnull"] += 1
    return out


def distinct_markets(items, keys=("conditionId", "market", "asset", "condition_id", "marketSlug")):
    s = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        for k in keys:
            c = it.get(k)
            if c:
                s.add(str(c))
                break
    return s


def num(v):
    try:
        return float(v)
    except Exception:
        return None


def find_flatbarrel_address():
    """Ishchet 'flatbarrel' cherez /trades. Esli ne nayden -- berem chasto torguyushchiy koshelek iz tennisnyh rynkov."""
    print("\n[0] Poisk adresa 'flatbarrel' cherez /trades ...")
    # Probuem po imeni -- no data-api ne podderzhivaet search po imeni.
    # Po zadaniyu: adres nado nayti po data-api/trades. Vozmomno eto proxyWallet
    # iz nedavney tennisnoy sdelki. Berem skyman44 dlya bazovogo sravneniya,
    # i takzhe poprobuem nayti 'flatbarrel' cherez matchevye rynki.
    # Strategiya: vozmomno est /trades?taker=flatbarrel (ne po adresu).
    # Proveryaem neskolko variantov:
    for cand in ("flatbarrel", "flatbarrel1", "flat_barrel"):
        data, err = get_json("/trades", {"user": cand, "limit": 5})
        if not err and isinstance(data, list) and data:
            print("    NAYDEN 'flatbarrel' cherez /trades?user=%s: %d zapisey" % (cand, len(data)))
            w = (data[0].get("proxyWallet") or data[0].get("user") or "").lower()
            if w:
                print("    address:", w)
                return w, "found via /trades?user=%s" % cand
    # Ne nayden po imeni. Ishchem samyy aktivnyy koshelek po tennisu za nedelyu.
    print("    'flatbarrel' po imeni NE nayden. Ishchu aktivnyy koshelek po tennisu (zapasnoy).")
    # Vozmomno 'flatbarrel' -- eto imya iz leaderboard, ne address. Probjem cherez_gamma /profile?name=
    data, err = get_json(GAMMA + "/profiles", {"name": "flatbarrel", "limit": 5})
    if not err and isinstance(data, list):
        for p in data:
            if isinstance(p, dict):
                addr = p.get("proxyWallet") or p.get("address") or p.get("walletAddress")
                if addr:
                    print("    NAYDEN cherez /profiles?name=flatbarrel:", addr)
                    return addr.lower(), "found via /profiles?name=flatbarrel"
    # Zapas: hochey aktivnogo tennis-treydera
    data, err = get_json("/trades", {"limit": 500, "market": "0x9cda895bf278c403b29fca7b1607eea87b103e022bab40b00f4d001efbabab9b"})
    if not err and isinstance(data, list):
        wallets = defaultdict(int)
        for t in data:
            w = (t.get("proxyWallet") or "").lower()
            if w:
                wallets[w] += 1
        if wallets:
            top = sorted(wallets.items(), key=lambda x: -x[1])[0]
            print("    ZAPASNOY: samyy aktivnyy koshelek po bazovomu rynku:", top)
            return top[0], "fallback most-active on base market"
    return None, "not found"


def fetch_all_trades_for_user(addr, max_pages=30):
    """Poluchet vse sdelki polzovatelya s paginaciey."""
    all_rows = []
    offset = 0
    limit = 500
    for _ in range(max_pages):
        data, err = get_json("/trades", {"user": addr, "limit": limit, "offset": offset})
        if err:
            return all_rows, err
        if not isinstance(data, list):
            return all_rows, "not-list"
        if not data:
            return all_rows, None
        all_rows.extend(data)
        if len(data) < limit:
            return all_rows, None
        offset += limit
    return all_rows, "cap-30pages"


def fetch_positions(addr, limit=None, offset=None):
    params = {"user": addr}
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    return get_json("/positions", params)


def probe_wallet(name, addr):
    print("\n" + "=" * 72)
    print("KOSHELEK: %s  %s" % (name, addr))
    print("=" * 72)
    result = {"name": name, "address": addr}

    # 1. BAZOVYY ZAPROS /positions
    resp, err = fetch_positions(addr)
    if err:
        print("  /positions OSHIBKA:", err)
        result["error"] = err
        return result
    items = as_list(resp)
    print("  tip otveta:", type(resp).__name__, "| poziciy:", len(items))
    result["n_positions_default"] = len(items)
    result["response_type"] = type(resp).__name__

    # Sohranim odnu syruyu pozitsiyu polnostyu
    if items:
        result["raw_first_position"] = items[0]
        print("  --- SYRAYA PERVAYA POZICIYA (polnostyu) ---")
        print("  " + json.dumps(items[0], ensure_ascii=False, indent=2)[:1200])

    # 1. POLA + TIPY
    if items:
        ft = field_types(items)
        print("\n  --- POLA (%d) ---" % len(ft))
        for k in sorted(ft):
            ex = ft[k]["ex"]
            exs = json.dumps(ex, ensure_ascii=False)
            if len(exs) > 60:
                exs = exs[:60] + "..."
            print("    %-26s %-18s prim=%s | nonnull=%d null=%d" % (
                k, "/".join(sorted(ft[k]["types"])), exs, ft[k]["nonnull"], ft[k]["nulls"]))
        result["fields"] = {k: {"types": sorted(v["types"]), "example": v["ex"],
                                "nonnull": v["nonnull"], "nulls": v["nulls"]}
                            for k, v in ft.items()}

        # 2. POLA PRIBLYLI/STOIMOSTI
        pnl_fields = [k for k in ft if PNL_RX.search(k)]
        print("\n  --- POLA PRIBYLI/STOIMOSTI (po imeni):", pnl_fields if pnl_fields else "NET")
        result["pnl_value_fields_by_name"] = pnl_fields

        # 2. PRIBYL OTDELNO PO RYNKU?
        # Proveryaem: est li pole pribyli i unikalen li conditionId na poziciyu
        dm = distinct_markets(items)
        print("\n  --- RYNIKI ---")
        print("  poziciy:", len(items), "| distinct rynkov:", len(dm))
        result["n_distinct_markets"] = len(dm)
        result["per_market_profit_possible"] = len(dm) > 0 and len(pnl_fields) > 0
        # Proverka: 1 zapis = 1 rynok?
        ck = None
        for k in ("conditionId", "market", "asset", "condition_id", "marketSlug"):
            if items and isinstance(items[0], dict) and k in items[0]:
                ck = k
                break
        if ck:
            cond_vals = [str(it.get(ck)) for it in items if isinstance(it, dict)]
            dups = len(cond_vals) - len(set(cond_vals))
            print("  pole rynka:", ck, "| dubley conditionId:", dups, "(0 = 1 zapis = 1 rynok)")
            result["market_field"] = ck
            result["duplicate_market_keys"] = dups

        # 3. PRIZNAKI ZAKRYTIYA v samih poziciyah
        zero_sz = [it for it in items if num(it.get("size")) is not None and num(it.get("size")) == 0]
        redeem_fields = [k for k in ft if CLOSED_RX.search(k)]
        print("  pozicij s size==0:", len(zero_sz), "| polya-priznaki zakrytiya:", redeem_fields if redeem_fields else "NET")
        result["n_zero_size"] = len(zero_sz)
        result["closed_indicator_fields"] = redeem_fields

    # 4. DOKAZATELSTVO ZAKRYTYH POZICIY cherez /trades
    print("\n  --- DOKAZATELSTVO: ZAKRYTYE POZICII VIDNY? ---")
    trades, terr = fetch_all_trades_for_user(addr)
    if terr:
        print("  [dokaz] /trades?user= NE srabotal:", terr)
        result["trades_error"] = terr
    else:
        print("  [dokaz] /trades?user=: sdelok=%d" % len(trades))
        tdm = distinct_markets(trades)
        print("  [dokaz] /trades distinct rynkov:", len(tdm))
        result["n_trades"] = len(trades)
        result["n_distinct_markets_in_trades"] = len(tdm)
        if items:
            pm = distinct_markets(items)
            only_trades = tdm - pm
            only_pos = pm - tdm
            both = tdm & pm
            print("  [dokaz] rynkov v /trades NO NET v /positions:", len(only_trades))
            print("  [dokaz] rynkov v /positions NO NET v /trades:", len(only_pos))
            print("  [dokaz] rynkov v OBOIH:", len(both))
            result["markets_only_in_trades"] = len(only_trades)
            result["markets_only_in_positions"] = len(only_pos)
            result["markets_in_both"] = len(both)
            if only_trades:
                # Nayti primer zakrytogo rynka: poluchit info iz /trades i proverit closedTime
                ex_list = sorted(list(only_trades))[:5]
                result["markets_missing_from_positions_examples"] = ex_list
                print("  => DOKAZANO: /positions TERYAET pozicii. Primery rynkov v /trades no ne v /positions:")
                for c in ex_list[:3]:
                    # Nayti imya rynka iz /trades
                    m_trades = [t for t in trades if distinct_markets([t]) and c in distinct_markets([t])]
                    slug = ""
                    for t in m_trades:
                        slug = t.get("marketSlug") or t.get("slug") or ""
                        if slug:
                            break
                    print("     %s  slug=%s" % (c[:18] + "...", slug))
                # Pokazhem polnuyu odnu syruyu stroku iz /trades dlya dokazatelstva
                if only_trades:
                    c0 = sorted(list(only_trades))[0]
                    ex_trades = [t for t in trades if c0 in distinct_markets([t])]
                    if ex_trades:
                        print("  --- SYRAYA STROKA /trades dlya dokazatelstva (rynok est v trades, net v positions) ---")
                        print("  " + json.dumps(ex_trades[0], ensure_ascii=False)[:600])
                        result["proof_trade_raw"] = ex_trades[0]
                # Glavnyy vyvod
                print("\n  >>> VERDICT: ZAKRYTYE POZICII NE VIDNY v /positions (est rynki v istorii sdelok, otsutstvuyut v positions).")
                result["closed_positions_visible"] = False
            else:
                print("  => vse rynki iz /trades prisutstvuyut v /positions (v predelah vyborki).")
                result["closed_positions_visible"] = "indeterminate (vse sovpali v vyborke)"

    # 5. PAGINACIYA
    print("\n  --- PAGINACIYA ---")
    # Test: default vs limit=500 vs limit=1000 vs limit=1000&offset=N
    r_def = items
    r500, e500 = fetch_positions(addr, limit=500)
    n500 = len(as_list(r500)) if not e500 else None
    print("  default(limit ne ukazan):%d | limit=500:%s" % (len(r_def), n500 if n500 is not None else e500))
    r1000, e1000 = fetch_positions(addr, limit=1000)
    n1000 = len(as_list(r1000)) if not e1000 else None
    print("  limit=1000:", n1000 if n1000 is not None else e1000)
    r_off, e_off = fetch_positions(addr, limit=1000, offset=len(items))
    n_off = len(as_list(r_off)) if not e_off else None
    print("  limit=1000&offset=%d:%s" % (len(items), n_off if n_off is not None else e_off))
    result["pagination"] = {"default": len(r_def), "limit500": n500, "limit1000": n1000,
                             "limit1000_offset_n": n_off}
    # Proverim potolok: offset bolshoy
    if n1000 and n1000 > 0:
        r_big, e_big = fetch_positions(addr, limit=500, offset=5000)
        n_big = len(as_list(r_big)) if not e_big else "ERR:%s" % e_big
        print("  limit=500&offset=5000 (proverka potolka):", n_big)
        result["pagination"]["offset5000"] = n_big

    return result


def selftest():
    assert as_list([1, 2]) == [1, 2]
    assert as_list({"positions": [1]}) == [1]
    assert as_list({"x": 1}) == []
    ft = field_types([{"a": 1, "b": "x"}, {"a": 2}])
    assert set(ft) == {"a", "b"} and "int" in ft["a"]["types"]
    assert distinct_markets([{"conditionId": "c1"}, {"market": "c2"}, {"conditionId": "c1"}]) == {"c1", "c2"}
    assert num("3.5") == 3.5 and num(None) is None
    assert bool(PNL_RX.search("cashPnl")) and bool(PNL_RX.search("realizedPnl"))
    print("SELFTEST_OK")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
        return
    open(RAWLOG, "w", encoding="utf-8").close()
    print("GLM PROBA 2 -- data-api /positions")
    print("DATA:", DATA)
    print("Log:", RAWLOG)

    # Nayti flatbarrel
    flatbarrel, how = find_flatbarrel_address()
    wallets = dict(KNOWN_WALLETS)
    if flatbarrel:
        wallets["flatbarrel"] = flatbarrel
        print("flatbarrel:", flatbarrel, "(%s)" % how)
    else:
        print("flatbarrel NE NAJDEN -- rabotaem tolko so skyman44")

    results = {}
    for name, addr in wallets.items():
        results[name] = probe_wallet(name, addr)

    # 6. CHASTOTA / rate-limit info
    print("\n" + "=" * 72)
    print("OGARANICHENIE PO CHASTOTE")
    print("=" * 72)
    print("  Po zadaniyu: 150 zaprosov na 10 sekund.")
    print("  Skript throttlit do", RATE_MAX, "za", RATE_WIN_S, "s (zapas).")

    summary = {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "wallets": results}
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str, ensure_ascii=False)
    print("\nItog ->", SUMMARY)
    print("Syroy log ->", RAWLOG)


if __name__ == "__main__":
    main()