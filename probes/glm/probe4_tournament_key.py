#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PROBA 4 (GLM) -- otricatelnyy kontrol k probe3: poisk klyucha turnira.

probe3 sravnil dva matcha ODNOGO turnira i poluchil 12 "sovpadayuschih"
poley bez kontrolya. Polya-konstanty (sport, period, ended, endDate) i
polya-tiry (seriesSlug=atp/wta/itf) otlichit ot nastoyaschego klyucha
mozhno tolko otricatelnym kontrolem: vzyat match C iz DRUGOGO turnira v
TOT ZHE den UTC. Nastoyaschiy klyuch turnira OBYAZAN razlichatsya mezhdu
A i C; pole, odinakovoe u A i C, klyuchom byt ne mozhet.

Plan:
  1. /events?tag_slug=tennis&closed=false -> 6 matchey:
     - para AB: odin turnir, odin den UTC
     - para CD: DRUGOY turnir, tot zhe den UTC
     - para EF: raznye dni UTC (po vozmozhnosti odin turnir)
     Turnir opredelyaem po title (text do ':'), NE po polyu-kandidatu.
  2. Dlya kazhdogo polya (event+market, s prefiksom ev:/mk:) schitaem:
     same_ab, same_cd, diff_ac.
  3. Klassifikaciya:
     - KANDIDAT  : same_ab AND same_cd AND diff_ac
     - KONSTANTA : same_ab AND same_cd AND NOT diff_ac (odinakovo i u A,C)
     - SHUM      : inache (razlichaetsya vnutri pary)
  4. Pechat tri spiska + itog odnoy strokoy.

Granicy: tolko probes/glm/. Bez f-strok, ASCII-only vyvod.
Logi v probes/glm/probe4_raw_api.log.

usage:
  python probes/glm/probe4_tournament_key.py
  python probes/glm/probe4_tournament_key.py selftest
"""

import json
import os
import re
import sys
import time
import collections
import threading
import urllib.request
import urllib.parse
import urllib.error

# ─── Konfiguraciya ───────────────────────────────────────────────
GAMMA = os.environ.get("PM_GAMMA_HOST", "https://gamma-api.polymarket.com")
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)
REQ_TIMEOUT = 45.0
OFFSET_CAP = 2000
RATE_WIN_S = 10.0
RATE_MAX = 140

SINGLES_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
DOUBLES_RE = re.compile(r"doubles", re.I)
TIER_RES = [
    ("ATP", re.compile(r"^atp-", re.I)),
    ("WTA", re.compile(r"^wta-", re.I)),
    ("ITF", re.compile(r"^itf", re.I)),
]

LOGDIR = os.path.dirname(os.path.abspath(__file__))
RAWLOG = os.path.join(LOGDIR, "probe4_raw_api.log")

# Polya, soderzhaschie nazvanie turnira kak tekst (ne ID) -- ne schitayutsya
# nastoyaschim klyuchom, hotya formalno mogut byt' v kandidatah.
TEXT_FIELDS_HINT = {
    "ev:title", "ev:slug", "ev:description", "ev:longDescription",
    "mk:slug", "mk:question", "mk:groupItemTitle",
}
# URL kartinki turnira -- odinakovyy vnutri turnira, no ne ID.
MEDIA_FIELDS_HINT = {
    "ev:icon", "ev:image", "ev:headerImage", "mk:icon",
    "ev:imageBanner", "ev:imageCard", "ev:startDate",
}
# Znacheniy, sootvetstvuyuschih tiru (ATP/WTA/ITF), a ne turniru.
TIER_VALUES = {"atp", "wta", "itf", "atp tour", "wta tour", "itf tour"}
ID_HINT_KEYWORDS = ("id", "tournament", "series", "parent", "group", "league")

# ─── Potokobezopasnyy throttle ──────────────────────────────────
_win = collections.deque()
_lock = threading.Lock()


def throttle():
    while True:
        with _lock:
            now = time.time()
            while _win and now - _win[0] > RATE_WIN_S:
                _win.popleft()
            if len(_win) < RATE_MAX:
                _win.append(now)
                return
            wait = RATE_WIN_S - (now - _win[0]) + 0.02
        time.sleep(max(0.0, wait))


def log_raw(label, url, status, body_len=0):
    with open(RAWLOG, "a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"ts": time.time(), "label": label, "url": url,
                 "status": status, "body_len": body_len},
                default=str,
            )
            + "\n"
        )


def get_json(url, retries=4):
    last_err = None
    for attempt in range(retries):
        throttle()
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
                body = r.read().decode("utf-8", "replace")
                log_raw("GET", url, r.status, len(body))
                return json.loads(body), None
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            log_raw("GET-ERR", url, e.code, len(body))
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(min(2 ** attempt, 15))
                last_err = "HTTP %d" % e.code
                continue
            return None, "HTTP %d: %s" % (e.code, body[:200])
        except urllib.error.URLError as e:
            log_raw("GET-ERR", url, -1, 0)
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
                last_err = "URLError: %s" % e.reason
                continue
            return None, "URLError: %s" % e.reason
        except Exception as e:
            log_raw("GET-ERR", url, -1, 0)
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt, 8))
                last_err = "%s: %s" % (type(e).__name__, e)
                continue
            return None, "%s: %s" % (type(e).__name__, e)
    return None, last_err or "exhausted"


# ─── Utility ─────────────────────────────────────────────────────
def tier_of(slug):
    s = (slug or "").lower()
    if DOUBLES_RE.search(s):
        return "DOUBLES"
    for name, rx in TIER_RES:
        if rx.search(s):
            return name
    return "OTHER"


def tournament_from_title(title):
    """Turnir po title (text do poslednego ':'). Ne polya-kandidat."""
    if not title or not isinstance(title, str):
        return None
    if ":" in title:
        return title.rsplit(":", 1)[0].strip()
    return None


def extract_match(event):
    """Pervyy matchevyy rynok (singles, ne doubles, tier ATP/WTA/ITF)."""
    mkts = event.get("markets")
    if not isinstance(mkts, list):
        return None, None
    for m in mkts:
        if not isinstance(m, dict):
            continue
        slug = m.get("slug") or ""
        if not slug:
            continue
        if DOUBLES_RE.search(slug):
            continue
        if not SINGLES_RE.search(slug):
            continue
        tier = tier_of(slug)
        if tier in ("DOUBLES", "OTHER"):
            continue
        return m, tier
    return None, None


def date_utc_from(market):
    """Kalendarnaya data UTC iz gameStartTime ili sluga rynka."""
    gst = market.get("gameStartTime")
    if isinstance(gst, str) and len(gst) >= 10:
        return gst[:10]
    slug = market.get("slug") or ""
    mm = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    if mm:
        return mm.group(1)
    return None


# ─── API ─────────────────────────────────────────────────────────
def fetch_open_tennis_matches(max_pages=20):
    """/events?tag_slug=tennis&closed=false s paginaciey.
    Vozvraschaet spisok {"event","market","tier","tournament","date_utc","title"}.
    """
    matches = []
    seen_ids = set()
    offset = 0
    for _ in range(max_pages):
        params = {
            "limit": 100, "offset": offset, "tag_slug": "tennis",
            "closed": "false", "order": "endDate", "ascending": "true",
        }
        url = GAMMA + "/events?" + urllib.parse.urlencode(params)
        data, err = get_json(url)
        if err:
            print("    [fetch] ERR offset %d: %s" % (offset, err))
            break
        if not isinstance(data, list) or not data:
            break
        for ev in data:
            if not isinstance(ev, dict):
                continue
            eid = ev.get("id")
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
            m, tier = extract_match(ev)
            if not m:
                continue
            title = ev.get("title") or ""
            tname = tournament_from_title(title)
            date_utc = date_utc_from(m)
            matches.append({
                "event": ev, "market": m, "tier": tier,
                "tournament": tname, "date_utc": date_utc, "title": title,
            })
        if len(data) < 100:
            break
        offset += 100
        if offset >= OFFSET_CAP:
            break
    return matches


# ─── Vybor par ───────────────────────────────────────────────────
def select_pairs(matches):
    """AB: odin turnir + den (>=2 matcha).
    CD: DRUGOY turnir, tot zhe den (>=2 matcha), zhelatelno odnogo tira.
    EF: raznye dni, zhelatelno odin turnir.
    Vozvraschaet dict s A,B,C,D,E,F (lyuboy mozhet byt None) + metki.
    """
    # (turnir, data, tier) -> [matchi] -- tier v klyache obespechivaet
    # odnorodnost: vnutri gruppy vse matchi odnogo tira po definitsii.
    by_tdt = collections.defaultdict(list)
    for m in matches:
        if m["tournament"] and m["date_utc"]:
            by_tdt[(m["tournament"], m["date_utc"], m["tier"])].append(m)

    # Tolko gruppy s >=2 matchami odnogo tira
    valid = {k: v for k, v in by_tdt.items() if len(v) >= 2}

    # data -> tier -> [(turnir, [matchi]), ...]
    by_date_tier = collections.defaultdict(
        lambda: collections.defaultdict(list))
    for (tour, date, tier), ms in valid.items():
        by_date_tier[date][tier].append((tour, ms))

    # Ischem datu s dvumya raznymi turnirami ODNOGO tira,
    # kazhdyy s >=2 matchami. Zapasnogo varianta s raznymi tirami NET.
    best = None  # (date, tour1, tour2, tier, ms1, ms2)
    for date in sorted(by_date_tier.keys()):
        for tier, tour_list in by_date_tier[date].items():
            if len(tour_list) >= 2:
                t1, ms1 = tour_list[0]
                t2, ms2 = tour_list[1]
                best = (date, t1, t2, tier, ms1, ms2)
                break
        if best:
            break

    A = B = C = D = None
    target_date = tour1 = tour2 = target_tier = None
    if best:
        target_date, tour1, tour2, target_tier, ms1, ms2 = best
        A, B = ms1[0], ms1[1]
        C, D = ms2[0], ms2[1]

    # EF: odin turnir, raznye dni
    by_tour_dates = collections.defaultdict(dict)
    for m in matches:
        if m["tournament"] and m["date_utc"]:
            by_tour_dates[m["tournament"]].setdefault(m["date_utc"], []).append(m)

    E = F = None
    ef_same_tour = False
    for tour in sorted(by_tour_dates.keys()):
        dates = by_tour_dates[tour]
        ds = sorted(dates.keys())
        if len(ds) >= 2:
            E = dates[ds[0]][0]
            F = dates[ds[1]][0]
            ef_same_tour = True
            break

    # Zapas: raznye dni lyubye
    if not (E and F):
        all_dates = sorted({m["date_utc"] for m in matches if m["date_utc"]})
        if len(all_dates) >= 2:
            d1, d2 = all_dates[0], all_dates[1]
            for m in matches:
                if m["date_utc"] == d1 and E is None:
                    E = m
                elif m["date_utc"] == d2 and F is None and m is not E:
                    F = m
            ef_same_tour = bool(E and F and E["tournament"] == F["tournament"])

    return {
        "A": A, "B": B, "C": C, "D": D, "E": E, "F": F,
        "target_date": target_date, "tour1": tour1, "tour2": tour2,
        "target_tier": target_tier, "ef_same_tour": ef_same_tour,
    }


# ─── Klassifikaciya poley ───────────────────────────────────────
def flatten(ev, m):
    """Obedinyaet event (ev:) i market (mk:) polya."""
    out = {}
    if isinstance(ev, dict):
        for k, v in ev.items():
            out["ev:" + str(k)] = v
    if isinstance(m, dict):
        for k, v in m.items():
            out["mk:" + str(k)] = v
    return out


def value_key(v):
    """Stabilnoe predstavlenie znacheniya dlya sravneniya."""
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True, ensure_ascii=True)
    return json.dumps(v, ensure_ascii=True)


def value_is_tier(v):
    if isinstance(v, str):
        return v.strip().lower() in TIER_VALUES
    return False


def looks_like_id_field(name):
    low = name.lower()
    if "outcome" in low:
        return False
    return any(kw in low for kw in ID_HINT_KEYWORDS)


def classify_fields(pairs):
    """Klassificiruet polya po treh kategoriyam. Vozvraschaet dict ili None."""
    A = pairs["A"]; B = pairs["B"]; C = pairs["C"]; D = pairs["D"]
    E = pairs["E"]; F = pairs["F"]
    if not (A and B and C and D):
        return None

    fa = flatten(A["event"], A["market"])
    fb = flatten(B["event"], B["market"])
    fc = flatten(C["event"], C["market"])
    fd = flatten(D["event"], D["market"])
    fe = flatten(E["event"], E["market"]) if E else {}
    ff = flatten(F["event"], F["market"]) if F else {}

    all_fields = sorted(set(fa) | set(fb) | set(fc) | set(fd))

    candidates = []
    constants = []
    noise = []

    for f in all_fields:
        va = value_key(fa.get(f))
        vb = value_key(fb.get(f))
        vc = value_key(fc.get(f))
        vd = value_key(fd.get(f))
        same_ab = (va == vb)
        same_cd = (vc == vd)
        diff_ac = (va != vc)

        same_ef = None
        if E and F:
            same_ef = (value_key(fe.get(f)) == value_key(ff.get(f)))

        if same_ab and same_cd:
            if diff_ac:
                candidates.append((f, fa.get(f), same_ef))
            else:
                constants.append((f, fa.get(f)))
        else:
            reason = []
            if not same_ab:
                reason.append("diff_in_AB")
            if not same_cd:
                reason.append("diff_in_CD")
            noise.append((f, "+".join(reason)))

    return {
        "candidates": candidates,
        "constants": constants,
        "noise": noise,
        "has_ef": bool(E and F),
    }


def is_real_id_candidate(field, value):
    """True, esli pole ne text/media/tier i vyglyadit kak ID."""
    if field in TEXT_FIELDS_HINT or field in MEDIA_FIELDS_HINT:
        return False
    if value_is_tier(value):
        return False
    return looks_like_id_field(field)


def truncate(s, n=90):
    if not isinstance(s, str):
        s = json.dumps(s, ensure_ascii=True)
    return s if len(s) <= n else s[:n] + "..."


def field_hint(field, value):
    """Pometka tipa polya dlya pechati."""
    if field in TEXT_FIELDS_HINT:
        return " [TEXT]"
    if field in MEDIA_FIELDS_HINT:
        return " [MEDIA]"
    if value_is_tier(value):
        return " [TIER-VALUE]"
    if looks_like_id_field(field):
        return " [ID-LIKE]"
    return ""


# ─── Main ────────────────────────────────────────────────────────
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
        return

    open(RAWLOG, "w", encoding="utf-8").close()
    print("=" * 72)
    print("GLM PROBA 4 -- otricatelnyy kontrol: poisk klyucha turnira")
    print("GAMMA:", GAMMA)
    print("Raw API log:", RAWLOG)
    print("=" * 72)

    print("\n[1] Zagruzka otkrytyh tennisnyh matchey (closed=false)...")
    t0 = time.time()
    matches = fetch_open_tennis_matches(max_pages=20)
    print("    Zagruzeno matchey: %d (%.1f s)" % (len(matches), time.time() - t0))

    if len(matches) < 4:
        print("    MALO DANNYH: nuzhno >=4 matcha, est %d. Ostanovka." % len(matches))
        print("\nKLYUCH NE NAYDEN")
        return

    print("\n[2] Vybor par AB / CD / EF...")
    pairs = select_pairs(matches)
    A, B, C, D = pairs["A"], pairs["B"], pairs["C"], pairs["D"]
    E, F = pairs["E"], pairs["F"]

    if not (A and B and C and D):
        print("    NE UDALOS sobrat pary AB i CD iz raznyh turnirov v odin den.")
        print("    Naideno matchey: %d. Nuzhno, ctoby v odnu datu" % len(matches))
        print("    bylo >=2 turnira s >=2 matchami kazhdyy.")
        print("\nKLYUCH NE NAYDEN")
        return

    # ── 2a: pechat tira kazhdogo matcha otdelnoy strokoy ──
    print("    TIRY MATCHey (iz prefiksa sluga):")
    print("      A tier = %s" % A["tier"])
    print("      B tier = %s" % B["tier"])
    print("      C tier = %s" % C["tier"])
    print("      D tier = %s" % D["tier"])
    if E:
        print("      E tier = %s" % E["tier"])
    if F:
        print("      F tier = %s" % F["tier"])

    # ── 2b: ZHESKtKAYa proverka odnorodnosti tira A/B/C/D ──
    tiers_abcd = {A["tier"], B["tier"], C["tier"], D["tier"]}
    if len(tiers_abcd) != 1:
        print("\n    OTBOR NEVALIDEN: raznye tiry")
        print("    A/B/C/D tiry: %s, %s, %s, %s" % (
            A["tier"], B["tier"], C["tier"], D["tier"]))
        print("    Kontrol nevozmozhen: polya, razlichayuschie ATP/WTA,")
        print("    popadut v SHUM kak diff_in_CD i iskazyat rezultat.")
        print("\nKLYUCH NE NAYDEN")
        return

    print("    [kontrol chistyy]: A,B,C,D odnogo tira (%s)" % pairs["target_tier"])
    print("    -> tier-polya (series, seriesSlug, sport) idut v KONSTANTY")

    print("    Para AB (turnir=%s, data=%s):" % (pairs["tour1"], pairs["target_date"]))
    print("      A: %s | %s" % (A["title"], A["market"].get("slug")))
    print("      B: %s | %s" % (B["title"], B["market"].get("slug")))
    print("    Para CD (turnir=%s, data=%s):" % (pairs["tour2"], pairs["target_date"]))
    print("      C: %s | %s" % (C["title"], C["market"].get("slug")))
    print("      D: %s | %s" % (D["title"], D["market"].get("slug")))
    if E and F:
        tag = "odin turnir" if pairs["ef_same_tour"] else "raznye turniry"
        print("    Para EF (raznye dni, %s):" % tag)
        print("      E: %s | %s | %s" % (E["title"], E["date_utc"], E["market"].get("slug")))
        print("      F: %s | %s | %s" % (F["title"], F["date_utc"], F["market"].get("slug")))
    else:
        print("    Para EF: ne sobrana (malo dannyh po raznym dnym).")

    # ── 2c: doslovnye znacheniya shesti poley dlya A, B, C, D ──
    # Chtoby glazami videt, razlichayut li oni turniry, a ne ATP/WTA.
    CHECK_FIELDS = [
        ("ev:series", "series"), ("ev:seriesSlug", "seriesSlug"),
        ("ev:sport", "sport"), ("ev:resolutionSource", "resolutionSource"),
        ("ev:icon", "icon"), ("ev:image", "image"),
    ]
    print("\n    ZNACHENIYa 6 POLEY (A, B, C, D doslovno; icon/image -- posledniy segment URL):")
    fA = flatten(A["event"], A["market"])
    fB = flatten(B["event"], B["market"])
    fC = flatten(C["event"], C["market"])
    fD = flatten(D["event"], D["market"])

    def _url_tail(v):
        if isinstance(v, str) and "/" in v:
            return v.rstrip("/").rsplit("/", 1)[-1]
        return v

    for flat_key, label in CHECK_FIELDS:
        va = _url_tail(fA.get(flat_key))
        vb = _url_tail(fB.get(flat_key))
        vc = _url_tail(fC.get(flat_key))
        vd = _url_tail(fD.get(flat_key))
        print("      %-18s A=%s | B=%s | C=%s | D=%s" % (
            label, json.dumps(va, ensure_ascii=True),
            json.dumps(vb, ensure_ascii=True),
            json.dumps(vc, ensure_ascii=True),
            json.dumps(vd, ensure_ascii=True)))

    print("\n[3] Klassifikaciya poley...")
    res = classify_fields(pairs)
    if res is None:
        print("    OSHIBKA klassifikacii.")
        print("\nKLYUCH NE NAYDEN")
        return

    candidates = res["candidates"]
    constants = res["constants"]
    noise = res["noise"]

    print("\n" + "-" * 72)
    print("KANDIDATY (sovpadayut v AB i CD, razlichayutsya A vs C): %d" % len(candidates))
    print("-" * 72)
    if candidates:
        for f, v, same_ef in candidates:
            ef_tag = ""
            if same_ef is not None:
                ef_tag = " | EF same=%s" % same_ef
            print("  %s%s%s = %s" % (f, field_hint(f, v), ef_tag, truncate(v, 100)))
    else:
        print("  (net)")

    print("\n" + "-" * 72)
    print("KONSTANTY (sovpadayut i vnutri par, i mezhdu A i C): %d" % len(constants))
    print("-" * 72)
    for f, v in constants:
        print("  %s%s = %s" % (f, field_hint(f, v), truncate(v, 60)))

    print("\n" + "-" * 72)
    print("SHUM (razlichaetsya vnutri pary): %d" % len(noise))
    print("-" * 72)
    for f, reason in noise:
        print("  %s [%s]" % (f, reason))

    # ── Itog ─────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("ITOG")

    # Realnye kandidaty = ne text/media/tier, ID-podobnoe imya
    real_candidates = [
        (f, v) for f, v, _ in candidates
        if is_real_id_candidate(f, v)
    ]

    if real_candidates:
        print("Strukturnye kandidaty-klyuchi (ID-podobnye, ne text/media/tier):")
        for f, v in real_candidates:
            print("  %s = %s" % (f, truncate(v, 100)))
    else:
        # Pokazhem, chto iz formalnyh kandidatov otseeno
        excluded = []
        for f, v, _ in candidates:
            if f in TEXT_FIELDS_HINT:
                excluded.append("%s[TEXT]" % f)
            elif f in MEDIA_FIELDS_HINT:
                excluded.append("%s[MEDIA]" % f)
            elif value_is_tier(v):
                excluded.append("%s[TIER]" % f)
            else:
                excluded.append("%s[?] = %s" % (f, truncate(v, 40)))
        if excluded:
            print("Formalnye kandidaty otseeny (ne yavlyayutsya strukturnym ID):")
            for e in excluded:
                print("  %s" % e)
        else:
            print("Formalnyh kandidatov net voobshche.")

    # ── KLYUCH stroka (odna) ─────────────────────────────────────
    print("")
    if real_candidates:
        print("KLYUCH NAYDEN: %s" % real_candidates[0][0])
    else:
        print("KLYUCH NE NAYDEN")
    print("=" * 72)


# ─── Selftest ────────────────────────────────────────────────────
def _make_match(tour, player, tier="ATP", date="2026-08-03", extra_ev=None, extra_mk=None):
    ev = {
        "id": "ev-%s-%s" % (tour, player),
        "title": "%s: P%s vs X" % (tour, player),
        "slug": "%s-%s-%s" % (tier.lower(), player, date),
        "sport": "tennis",
        "seriesSlug": tier.lower(),
        "startDate": date + "T10:00:00Z",
    }
    mk = {
        "conditionId": "c-%s-%s" % (tour, player),
        "slug": "%s-p%s-%s" % (tier.lower(), player, date),
    }
    if extra_ev:
        ev.update(extra_ev)
    if extra_mk:
        mk.update(extra_mk)
    return {
        "event": ev, "market": mk, "tier": tier,
        "tournament": tour, "date_utc": date,
        "title": ev["title"],
    }


def selftest():
    # Regexp / utility
    assert SINGLES_RE.search("atp-p1-p2-2026-08-03")
    assert not SINGLES_RE.search("atp-final")
    assert DOUBLES_RE.search("atp-doubles-x-2026-08-03")
    assert tier_of("atp-p1-p2-2026-08-03") == "ATP"
    assert tier_of("wta-p1-p2-2026-08-03") == "WTA"
    assert tier_of("itf-p1-p2-2026-08-03") == "ITF"
    assert tournament_from_title("Merida Open: A vs B") == "Merida Open"
    assert tournament_from_title("No colon") is None

    # value_key / flatten
    assert value_key({"a": 1, "b": 2}) == value_key({"b": 2, "a": 1})
    assert value_key([1, 2]) != value_key([2, 1])
    fa = flatten({"id": "1", "title": "x"}, {"slug": "s", "q": "Q"})
    assert "ev:id" in fa and "mk:slug" in fa
    assert "ev:slug" not in fa and "mk:id" not in fa

    # value_is_tier / looks_like_id_field
    assert value_is_tier("atp") and value_is_tier("WTA")
    assert not value_is_tier("merida-open")
    assert looks_like_id_field("ev:tournamentId")
    assert looks_like_id_field("ev:seriesSlug")
    assert not looks_like_id_field("ev:title")
    assert not looks_like_id_field("mk:outcomeIndex")

    # ── Sinteticheskiy kontrol: dva turnira v odin den ──
    # T1 i T2 -- raznye turniry, oba ATP (odin tir).
    # Dobavlen polya tournamentId (klyuch turnira) i icon (media).
    A = _make_match("T1", "1", tier="ATP", date="2026-08-03",
                    extra_ev={"tournamentId": "tid-1", "icon": "https://x/t1.png"})
    B = _make_match("T1", "2", tier="ATP", date="2026-08-03",
                    extra_ev={"tournamentId": "tid-1", "icon": "https://x/t1.png"})
    C = _make_match("T2", "3", tier="ATP", date="2026-08-03",
                    extra_ev={"tournamentId": "tid-2", "icon": "https://x/t2.png"})
    D = _make_match("T2", "4", tier="ATP", date="2026-08-03",
                    extra_ev={"tournamentId": "tid-2", "icon": "https://x/t2.png"})
    E = _make_match("T1", "5", tier="ATP", date="2026-08-04",
                    extra_ev={"tournamentId": "tid-1", "icon": "https://x/t1.png"})
    F = _make_match("T1", "6", tier="ATP", date="2026-08-05",
                    extra_ev={"tournamentId": "tid-1", "icon": "https://x/t1.png"})

    pairs = {
        "A": A, "B": B, "C": C, "D": D, "E": E, "F": F,
        "target_date": "2026-08-03", "tour1": "T1", "tour2": "T2",
        "same_tier_ab_cd": True, "ef_same_tour": True,
    }
    res = classify_fields(pairs)
    assert res is not None

    cand_fields = {c[0] for c in res["candidates"]}
    const_fields = {c[0] for c in res["constants"]}
    noise_fields = {n[0] for n in res["noise"]}

    # ev:tournamentId -- nastoyaschiy klyuch: odinakov v AB i CD, razlichayetsya A vs C
    assert "ev:tournamentId" in cand_fields, \
        "tournamentId dolzhen byt kandidatom: %s" % cand_fields
    # ev:icon -- odinakov vnutri turnira, razlichayetsya mezhdu T1/T2 -- formalnyy kandidat,
    # no my ego otseem kak MEDIA
    assert "ev:icon" in cand_fields, "icon formalno kandidat: %s" % cand_fields
    # ev:sport i ev:seriesSlug -- konstanty (odinakovy u vseh ATP)
    assert "ev:sport" in const_fields, "sport dolzhen byt konstantoy"
    assert "ev:seriesSlug" in const_fields, "seriesSlug dolzhen byt konstantoy pri odnom tire"
    # ev:title, ev:id, mk:conditionId -- raznye u matchey odnogo turnira -> shum
    assert "ev:title" in noise_fields
    assert "ev:id" in noise_fields
    assert "mk:conditionId" in noise_fields

    # Realnyy ID-klyuch otsekaet text/media/tier
    real = [(f, v) for f, v, _ in res["candidates"] if is_real_id_candidate(f, v)]
    real_names = {f for f, _ in real}
    assert "ev:tournamentId" in real_names, "real dolzhen vklyuchat tournamentId"
    assert "ev:icon" not in real_names, "icon (MEDIA) dolzhen byt otsejon"
    # EF: klyuch turnira stabilen mezhdu dnyami (same_ef=True)
    for f, v, same_ef in res["candidates"]:
        if f == "ev:tournamentId":
            assert same_ef is True, "tournamentId dolzhen sovpadat v EF (odin turnir)"
        if f == "ev:icon":
            assert same_ef is True, "icon dolzhen sovpadat v EF (odin turnir)"

    # ── Kontrol: raznye tiry -> seriesSlug stanovitsya kandidatom, no otsekaetsya po znacheniyu ──
    Cw = _make_match("T2", "3", tier="WTA", date="2026-08-03",
                     extra_ev={"tournamentId": "tid-2", "icon": "https://x/t2.png"})
    Dw = _make_match("T2", "4", tier="WTA", date="2026-08-03",
                     extra_ev={"tournamentId": "tid-2", "icon": "https://x/t2.png"})
    pairs2 = dict(pairs)
    pairs2["C"] = Cw
    pairs2["D"] = Dw
    pairs2["same_tier_ab_cd"] = False
    res2 = classify_fields(pairs2)
    cand2 = {c[0] for c in res2["candidates"]}
    # seriesSlug teper kandidat (atp vs wta), no eto TIR, ne turnir
    assert "ev:seriesSlug" in cand2, "seriesSlug dolzhen byt kandidatom pri raznyh tirah"
    real2 = [(f, v) for f, v, _ in res2["candidates"] if is_real_id_candidate(f, v)]
    real2_names = {f for f, _ in real2}
    assert "ev:seriesSlug" not in real2_names, \
        "seriesSlug=TIR dolzhen byt otsejon kak tier-value, a ne klyuch turnira"
    # tournamentId vse esche realnyy klyuch
    assert "ev:tournamentId" in real2_names

    # throttle thread test
    errs = []

    def hammer():
        try:
            for _ in range(30):
                throttle()
        except Exception as e:
            errs.append(e)

    ts = [threading.Thread(target=hammer) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs, errs
    print("SELFTEST_OK")


if __name__ == "__main__":
    main()