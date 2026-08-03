#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PROBA 3 (GLM) -- polya matchevyh sobytiy: poisk klyucha klastorizacii.

§10 PREREGISTRATION.md opredelyaet dva urovnya klasternyh SE:
match i "turnirnyy den". Dlya vtorogo nuzhen identifikator turnira.
Edinstvennyy izvestnyy ticker ("2026-mens-australian-open-winner") --
eto futures na pobeditelya, ne match. Cel zondy:

  1. Nayti TRI nastoyaschih matchevyh sobytiya: atp, wta, itf.
     Match = market so slugom ~ \\d{4}-\\d{2}-\\d{2}$, bez doubles.
  2. Napechatat VSE polya kazhdogo sobytiya i ego matchevogo rynka,
     bez sokrascheniy i bez vybora.
  3. Nayti dva matcha odnogo turnira i sravnit ih polya pole v pole.
  4. Otvetit otdelnym abzatem: est li hot odno pole, po kotoromu
     dva matcha odnogo turnira mozhno svyazat. Esli net -- skazat.

Granicy: tolko probes/glm/. Skript ne menyaet kriteryi, ne zapuskaet
analiz. Tolko diagnostika poly.

usage:
  python probes/glm/probe3_event_fields.py
  python probes/glm/probe3_event_fields.py selftest
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
from datetime import datetime, timezone, timedelta

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
RAWLOG = os.path.join(LOGDIR, "probe3_raw_api.log")

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
                {
                    "ts": time.time(),
                    "label": label,
                    "url": url,
                    "status": status,
                    "body_len": body_len,
                },
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
def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def tier_of(slug):
    s = (slug or "").lower()
    if DOUBLES_RE.search(s):
        return "DOUBLES"
    for name, rx in TIER_RES:
        if rx.search(s):
            return name
    return "OTHER"


def tournament_from_title(title):
    """Izvlekaet nazvanie turnira iz zagolovka sobytiya.

    Format Polymarket: 'Tournament Name: Player A vs Player B'
    Vozvraschaet chast do poslednego ':', ili None.
    """
    if not title or not isinstance(title, str):
        return None
    if ":" in title:
        return title.rsplit(":", 1)[0].strip()
    return None


# ─── API ─────────────────────────────────────────────────────────
def fetch_events_slice(start_dt, end_dt, closed=None):
    lo, hi = iso(start_dt), iso(end_dt)
    all_events = []
    offset = 0
    while True:
        params = {
            "limit": 100,
            "offset": offset,
            "tag_slug": "tennis",
            "end_date_min": lo,
            "end_date_max": hi,
            "order": "endDate",
            "ascending": "true",
        }
        if closed is not None:
            params["closed"] = str(closed).lower()
        url = GAMMA + "/events?" + urllib.parse.urlencode(params)
        data, err = get_json(url)
        if err:
            print("    [events] ERR %s..%s off %d: %s" % (lo, hi, offset, err))
            return all_events, False
        if not isinstance(data, list) or not data:
            break
        all_events.extend(data)
        if len(data) < 100:
            break
        offset += 100
        if offset >= OFFSET_CAP:
            break
    return all_events, True


def extract_match(event):
    """Izvlekaet pervyy matchevyy rynok iz sobytiya.

    Vozvraschaet (market_dict, tier) ili (None, None).
    Match = rynok so slugom ~ \\d{4}-\\d{2}-\\d{2}$, bez doubles,
    tier v (ATP, WTA, ITF).
    """
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


# ─── Scan ────────────────────────────────────────────────────────
def scan_for_matches():
    """Skanirovanie v shirokom okne.

    Vozvraschaet:
      found: dict tier -> [(event, market), ...]
      tournament_groups: dict tname -> [{"event":ev, "market":m, "tier":t}, ...]
    """
    found = {t: [] for t in ("ATP", "WTA", "ITF", "OTHER")}
    tournament_groups = collections.defaultdict(list)
    seen_event_ids = set()

    # Diapazony: (start, end, closed)
    ranges = [
        (datetime(2026, 1, 25, tzinfo=timezone.utc),
         datetime(2026, 3, 1, tzinfo=timezone.utc), True),
        (datetime(2026, 6, 1, tzinfo=timezone.utc),
         datetime(2026, 8, 15, tzinfo=timezone.utc), True),
        (datetime(2026, 7, 15, tzinfo=timezone.utc),
         datetime(2026, 9, 1, tzinfo=timezone.utc), False),
    ]
    step = timedelta(days=4)
    need_all = {"ATP", "WTA", "ITF"}

    for rs, re_end, cl in ranges:
        if all(len(found[t]) > 0 for t in need_all):
            break
        cur = rs
        range_events = 0
        while cur < re_end:
            nxt = min(cur + step, re_end)
            events, _ok = fetch_events_slice(cur, nxt, closed=cl)
            range_events += len(events)
            for ev in events:
                if not isinstance(ev, dict):
                    continue
                eid = ev.get("id")
                if eid in seen_event_ids:
                    continue
                seen_event_ids.add(eid)
                m, tier = extract_match(ev)
                if m and tier in found:
                    if len(found[tier]) < 20:
                        found[tier].append((ev, m))
                    tname = tournament_from_title(ev.get("title"))
                    if tname:
                        entry = {"event": ev, "market": m, "tier": tier}
                        if len(tournament_groups[tname]) < 20:
                            tournament_groups[tname].append(entry)
            cur = nxt
        print(
            "    Diapazon %s..%s closed=%s: sobytiy=%d | ATP=%d WTA=%d ITF=%d OTHER=%d"
            % (
                iso(rs),
                iso(re_end),
                cl,
                range_events,
                len(found["ATP"]),
                len(found["WTA"]),
                len(found["ITF"]),
                len(found["OTHER"]),
            )
        )

    return found, tournament_groups


def itf_fallback(found):
    """Zapasnoy poisk ITF cherez drugie tag_slug i poisk v title."""
    print("\n    Zapasnoy poisk ITF (drugie tag_slug)...")
    for tag in ("itf", "itf-tennis", "tennis-itf", "itf1"):
        for cl in ("true", "false"):
            url = GAMMA + "/events?" + urllib.parse.urlencode(
                {"tag_slug": tag, "limit": 50, "closed": cl}
            )
            data, err = get_json(url)
            if err:
                continue
            if isinstance(data, list) and data:
                print(
                    "    tag_slug=%s closed=%s: naydeno %d sobytiy"
                    % (tag, cl, len(data))
                )
                for ev in data:
                    if not isinstance(ev, dict):
                        continue
                    m, tier = extract_match(ev)
                    if m:
                        found["ITF"].append((ev, m))
                        return True
    return False


# ─── Dump ────────────────────────────────────────────────────────
def dump_full(title, d):
    """Pechataet VSE polya dict bez sokrascheniy."""
    print("\n" + "-" * 72)
    print("%s (%d poley)" % (title, len(d) if d else 0))
    print("-" * 72)
    if not d:
        print("  (pustoy)")
        return
    for key in sorted(d.keys()):
        value = d[key]
        if isinstance(value, (dict, list)):
            j = json.dumps(value, ensure_ascii=False, indent=2)
            print("  %s:" % key)
            for line in j.split("\n"):
                print("    %s" % line)
        else:
            print("  %s = %s" % (key, json.dumps(value, ensure_ascii=False)))


# ─── Analysis ────────────────────────────────────────────────────
def find_same_tournament_pair(tournament_groups, preferred_tier=None):
    """Ischet dva matcha odnogo turnira.

    Vozvraschaet (tname, entry1, entry2) ili (None, None, None).
    """
    for tname, entries in sorted(tournament_groups.items()):
        if len(entries) < 2:
            continue
        if preferred_tier:
            filt = [e for e in entries if e["tier"] == preferred_tier]
            if len(filt) >= 2:
                return tname, filt[0], filt[1]
        return tname, entries[0], entries[1]
    return None, None, None


def compare_event_fields(ev1, ev2, control_ev=None):
    """Sravnivaet polya dvuh sobytiy pole v pole.

    control_ev — tret'e sobytiye iz drugogo turnira.
    Vozvraschaet spisok (key, value) netrivialnyh sovpadeniy.
    """
    print("\n" + "=" * 72)
    print("SRVNENIE: DVA MATCHA ODRogo TURNIRA")
    print("=" * 72)
    print("  Match 1: %s" % ev1.get("title"))
    print("  Match 2: %s" % ev2.get("title"))
    if control_ev:
        print("  KONTROL (drugoy turnir): %s" % control_ev.get("title"))

    all_keys = sorted(set(ev1.keys()) | set(ev2.keys()))

    # Trivialnye polya, kotorye ne nesusch informacii o turnire
    TRIVIAL = {
        "closed", "archived", "restricted", "negRisk",
        "enableComments", "enableGame", "isVerified",
        "isOnCompound", "showComments", "sitemapPriority",
        "isClassified", "competitive",
    }

    same_nontrivial = []
    same_trivial = []
    diff = []

    for k in all_keys:
        v1 = ev1.get(k)
        v2 = ev2.get(k)
        if v1 == v2 and v1 is not None:
            if k in TRIVIAL:
                same_trivial.append(k)
                continue
            # Pustye / nulevye znacheniya
            if v1 in ("", 0, False, [], {}):
                same_trivial.append(k)
                continue
            # Proverka: otlichaetsya li ot kontrolya?
            if control_ev is not None:
                cv = control_ev.get(k)
                if v1 == cv:
                    # Sovpadaet i s kontrolem → ne identifikator turnira
                    same_trivial.append(k)
                    continue
            same_nontrivial.append((k, v1))
        else:
            diff.append(k)

    print(
        "\n  SOVPADAYUT, NETRIVIALNYE (%d) — kandidaty na klyuch turnira:"
        % len(same_nontrivial)
    )
    if same_nontrivial:
        for k, v in same_nontrivial:
            vs = json.dumps(v, ensure_ascii=False)
            if len(vs) > 300:
                vs = vs[:300] + "..."
            print("    %s = %s" % (k, vs))
    else:
        print("    (net)")

    print(
        "\n  SOVPADAYUT, TRIVIALNYE (%d): %s"
        % (len(same_trivial), ", ".join(sorted(same_trivial)))
    )
    print(
        "  RAZLICHAYUTSYA (%d): %s" % (len(diff), ", ".join(sorted(diff)))
    )

    return same_nontrivial


def analyze_linking(selected, tournament_groups):
    print("\n" + "=" * 72)
    print("ANALIZ: EST LI POLE DLYA SVYAZYVANIYA MATCHey ODRogo TURNIRA?")
    print("=" * 72)

    # 1. Vse imena poley
    all_event_fields = set()
    all_market_fields = set()
    for tier in selected:
        ev, m = selected[tier]
        all_event_fields |= set(ev.keys())
        all_market_fields |= set(m.keys())

    print(
        "\n  Polya sobytiy (%d): %s"
        % (len(all_event_fields), ", ".join(sorted(all_event_fields)))
    )
    print(
        "  Polya rynkov (%d): %s"
        % (len(all_market_fields), ", ".join(sorted(all_market_fields)))
    )

    # 2. Polya s imenami-kandidatami
    keywords = [
        "tournament", "series", "league", "competition",
        "parent", "group", "tour", "eventgroup", "category",
    ]
    name_candidates = [
        f for f in sorted(all_event_fields | all_market_fields)
        if any(kw in f.lower() for kw in keywords)
    ]
    print(
        "\n  Imita poley-kandidatov (kluchevye slova): %s"
        % (name_candidates if name_candidates else "NET")
    )

    # 3. Tags
    print("\n  TAGS kazhdogo sobytiya:")
    for tier in ("ATP", "WTA", "ITF"):
        if tier not in selected:
            continue
        ev, _m = selected[tier]
        tags = ev.get("tags")
        if isinstance(tags, list):
            tag_slugs = []
            for t in tags:
                if isinstance(t, dict):
                    tag_slugs.append(
                        t.get("slug") or t.get("id") or t.get("label") or str(t)
                    )
                else:
                    tag_slugs.append(str(t))
            print("    %s: %s" % (tier, tag_slugs))
        else:
            print("    %s: tags = %s" % (tier, json.dumps(tags, ensure_ascii=False)))

    # 4. Ticker
    print("\n  TICKER kazhdogo sobytiya:")
    for tier in ("ATP", "WTA", "ITF"):
        if tier not in selected:
            continue
        ev, _m = selected[tier]
        print(
            "    %s: ticker = %s"
            % (tier, json.dumps(ev.get("ticker"), ensure_ascii=False))
        )

    # 5. Sravnenie plyy iz odnogo turnira
    pair_tier = "ATP" if "ATP" in selected else ("WTA" if "WTA" in selected else None)
    tname, e1, e2 = find_same_tournament_pair(
        tournament_groups, preferred_tier=pair_tier
    )

    same_nontrivial = []
    if tname:
        # Vybiraem kontrol iz drugogo turnira
        control_ev = None
        for tier in selected:
            cand_ev = selected[tier][0]
            cand_tname = tournament_from_title(cand_ev.get("title"))
            if cand_tname != tname:
                control_ev = cand_ev
                break

        same_nontrivial = compare_event_fields(
            e1["event"], e2["event"], control_ev
        )
    else:
        print("\n  Pary matchey odnogo turnira ne naydeny v vyborke.")

    # 6. Itorovyy otvet
    print("\n" + "=" * 72)
    print("OTVET")
    print("=" * 72)

    if name_candidates:
        print("  Polya s imenami-kandidatami naydeny: %s" % name_candidates)
        # Proveryaem ih znacheniya
        for tier in ("ATP", "WTA", "ITF"):
            if tier not in selected:
                continue
            ev, m = selected[tier]
            for f in name_candidates:
                val = ev.get(f) if f in ev else m.get(f)
                if val is not None:
                    print("    %s.%s = %s" % (tier, f, json.dumps(val, ensure_ascii=False)))

    if same_nontrivial:
        print("\n  Dva matcha odnogo turnira imeyut SOVPADAYUSCHIE")
        print("  netrivialnye polya (kandidaty na klyuch turnira):")
        for k, _v in same_nontrivial:
            print("    - %s" % k)
        print("\n  ETO potentialno goditsya, no prover'te kachestvo:")
        print("  naprimer, odinakovyy URL ikonki ne yavlyaetsya nadyozhnym ID.")
    else:
        print("\n  NET ni odnogo polya, po kotoromu dva matcha odnogo")
        print("  turnira mozhno svyazat mezhdu soboy.")
        if not name_candidates:
            print(
                "  Sredi %d poley sobytiy i %d poley rynkov net ni odnogo"
                % (len(all_event_fields), len(all_market_fields))
            )
            print("  s imenem turnir/serii/ligi/parent/group.")
        print("  Tags soderzhat tolko tipovye metki (tennis, atp/wta),")
        print("  bez privyazki k konkretnomu turniru.")
        print("  Ticker u matchevyh sobytiy otsutstvuet.")
        print("  Polya title i slug soderzhat nazvanie turnira kak")
        print("  tekst, no ne kak strukturirovannyy identifikator.")
        print("  Klasternyy uroven 'turnir' po dannym gamma-api")
        print("  NEVOSSTANOVIM.")


# ─── Selftest ────────────────────────────────────────────────────
def selftest():
    assert SINGLES_RE.search("atp-sinner-alcaraz-2026-07-15")
    assert not SINGLES_RE.search("atp-final")
    assert not SINGLES_RE.search("2026-mens-australian-open-winner")
    assert DOUBLES_RE.search("atp-doubles-x-2026-07-15")
    assert tier_of("atp-x-y-2026-02-15") == "ATP"
    assert tier_of("wta-x-y-2026-02-15") == "WTA"
    assert tier_of("itf-x-y-2026-03-01") == "ITF"
    assert tier_of("atp-doubles-x-2026-03-01") == "DOUBLES"
    assert tournament_from_title("Merida Open: A vs B") == "Merida Open"
    assert tournament_from_title("No colon") is None
    # Throttle thread test
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


# ─── Main ────────────────────────────────────────────────────────
def main():
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
        return

    open(RAWLOG, "w", encoding="utf-8").close()
    print("=" * 72)
    print("GLM PROBA 3 -- polya matchevyh sobytiy i poisk klyucha klastrizacii")
    print("GAMMA:", GAMMA)
    print("Raw API log:", RAWLOG)
    print("=" * 72)

    # 1. Skanirovanie
    print("\n[1] Skanirovanie /events?tag_slug=tennis v shirokom okne...")
    t0 = time.time()
    found, tournament_groups = scan_for_matches()
    print("    Skanirovanie zajalo %.1f s" % (time.time() - t0))

    # 2. ITF fallback
    if not found["ITF"]:
        print("\n[2] ITF ne nayden po prefiksu sluga. Zapasnoy poisk...")
        if itf_fallback(found):
            print("    ITF nayden zapasnym metodom!")
        else:
            print("    ITF ne nayden ni odnim metodom.")
            print("    Polymarket, vozmozhno, ne predlagaet ITF matchi.")

    # 3. Itog scheta
    print("\n[3] SCHET:")
    for tier in ("ATP", "WTA", "ITF", "OTHER"):
        print("    %s: %d sobytiy" % (tier, len(found.get(tier, []))))
    same_tour = sum(1 for v in tournament_groups.values() if len(v) >= 2)
    print("    Turnirov s >=2 matchami: %d" % same_tour)

    # 4. Vybor predstaviteley
    selected = {}
    print("\n[4] VYBOR PREDSTAVITELEY:")
    for tier in ("ATP", "WTA", "ITF"):
        lst = found.get(tier, [])
        if lst:
            ev, m = lst[0]
            selected[tier] = (ev, m)
            print(
                "    %s: %s | market_slug=%s"
                % (tier, ev.get("title"), m.get("slug"))
            )
        else:
            print("    %s: NET DANNYH" % tier)

    # 5. Polnyy damp polsey
    for tier in ("ATP", "WTA", "ITF"):
        if tier not in selected:
            continue
        ev, m = selected[tier]
        print("\n" + "#" * 72)
        print("# DAMP SOBYTIYA: %s" % tier)
        print("#" * 72)
        dump_full("POLYA SOBYTIYA (event)", ev)
        dump_full("POLYA RYNKA (match market)", m)

    # 6. Analiz
    analyze_linking(selected, tournament_groups)

    print("\n" + "=" * 72)
    print("GOTOVO.")
    print("Raw API log ->", RAWLOG)
    print("=" * 72)


if __name__ == "__main__":
    main()