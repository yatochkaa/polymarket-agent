#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect_window_v1.py -- SBOR po netronutomu oknu 2026-02-01 .. 2026-04-28.

ZADACHA (razblokirovana zamorozkoy dat POPRAVKA7 sec.7b):
  - Kandidaty ishchutsya ZANOVO, s nulya. Spisok 1406 NE ispolzuetsya nikak
    (ni kak vhod, ni kak sverka) -- on voobshche ne chitaetsya etim skriptom.
  - Tolko ATP i WTA, odinochnye matchi (maska daty v slage: -YYYY-MM-DD$).
  - Otbor matchey po gameStartTime vnutri granic okna (poluotkrytyy [start, end_excl)).
  - Voronka po sec.7d: 5 strok otseva + stroka sostava tirov DO otbora koshelkov.
  - Odin progon. FAIL-FAST: pri lyubom neodnoznachnom VHODE skript PADAET (raise),
    a ne molcha propuskaet. Opredelyonnye vybytiya (net gst, net etalona, N=0 i t.d.)
    -- eto NE oshibka vhoda, a stroki voronki, oni schitayutsya.
  - Temp zaprosov: data-api/trades limit 200/10s na IP (svyazyvayushchiy);
    derzhim 140/10s obshchim throttle na vse zaprosy (gamma+data-api, odin IP).

PRAVKA 1 (03.08): enum end_date span = 2026-01-01 .. 2026-06-02 (zapas +35, kak probe1).
                  Chlenstvo v okne po-prezhnemu STROGO po gameStartTime.
KONTROL (03.08): srazu posle enumeracii ATP+WTA v okne dolzhno byt' ~4068
                 (polnyy perebor: okno [02-01,05-01)=4285, minus V2 217 = 4068).
                 Dopustimo 4050..4110. Vne diapazona -> PADAET DO sbora.
PRAVKA 2 (03.08): retry-klassifikaciya. HTTP 408/429/500/502/503/504 -> do 5 povtorov
                  s narastayushchey pauzoy; ischerpano -> padenie s ukazaniem rynka i
                  smeshcheniya. Vsyo ostalnoe (v t.ch. HTTP 400, conn-otkaz) -> padenie SRAZU.
                  Chislo uspeshnyh retraev pechataetsya v otchyote.
PAGINACIYA /trades (proven diag_pass2_recover3d.py): limit<=10000, offset<=10000 (hard cap).
                  Max dostizhimo = 20000 svezhih sdelok za 2 zaprosa. Feed = DESC (novye
                  vperyod), predmatch sdelki = SAMYE STARYE. Polnota DOKAZUETSYA tolko esli
                  poslednyaya stranica < limit (doshli do istinnogo nachala istorii). Esli obe
                  stranicy polnye (>=20000, hvost staryh NEDOSTIZHIM) -> predmatch nepolon ->
                  FAIL-FAST (spisok takih rynkov v konce). NE molchalivoe usechenie.

UTOCHNENIE sec.5 -- DVA raznyh poroga (03.08):
  DOPUSK TIRA   (do otbora koshelkov, pro DANNYE okna): v tire >=100 matchey v okne
                I razmah >=60 dney. Provalilsya reshayushchiy tir -> okno NEPRIGODNO (sec.7e).
  DOPUSK KOSHELKA (pro koshelyok, VNUTRI odnogo tira, tiry ne smeshivayutsya):
                u koshelka >=100 deystvitelnyh par I razmah >=60 dney. Koshelyok mozhet
                proyti po ATP i ne proyti po WTA -- eto norma; v semyu FDR vhodit odin raz
                po KAZHDOMU proshedshemu tiru.

DOBAVKA 1 (03.08): parquet po KAZHDOY pare koshelyok x match:
  wallet, slug, tier, gameStartTime, n_trades, entry_vwap, p_ref, p_ref_age_min, clv,
  dropped_reason (pusto -> para proshla). -> lyuboy pereschyot = chtenie s diska.
DOBAVKA 2 (03.08): rezhim dryrun -- suhoy progon na sreze 2026-02-01..2026-02-07, ves
  konveyer, vse stroki voronki. NE schitaetsya edinstvennym progonom sec.7 (okno drugoe,
  rezultat vybrasyvaetsya). Kontrol 4068 v dryrun NE primenyaetsya.
DOBAVKA 3 (03.08): etalon p_ref ISKLYUCHAET sdelki po terminalnym cenam (>=0.999 / <=0.001)
  iz vybora opornoy ceny (oni raschyotnye). Chislo takih cen VNUTRI predmatch okna schitaetsya
  i pechataetsya (ozhidaetsya 0).

OPREDELENIYA (verbatim iz zamorozhennogo POPRAVKA7):
  sec.2 etalon  = poslednyaya sdelka RYNKA (lyuboy koshelyok) strogo ranshe gst, cherez
                  komplement (cena storony idx0 = price esli idx0, inache 1-price);
                  predelnyy vozrast etalona -- 60 minut do starta. (+DOBAVKA3: bez term-cen)
  sec.3 svyortka: T* = outcome index 0 po poryadku clobTokenIds.
                  BUY T* s@p->+s@p ; SELL T* s@p->-s@p ; BUY compl s@q->-s@(1-q) ;
                  SELL compl s@q->+s@(1-q). N=znakovaya summa; N=0 -> para vybyvaet.
                  cena vhoda = VWAP TOLKO po sdelkam v storonu chistoy pozicii.
                  clobTokenIds ne rovno 2 / poryadok nedostupen -> PADAET.
  sec.4 clv     = (long T*) p_ref - entry ; (short T*) entry - p_ref.
  term/winner   = terminalnyy pobeditel po POSLEDNEY sdelke kazhdogo tokena:
                  p0>=0.9&p1<=0.1->0; p1>=0.9&p0<=0.1->1; inache None (stroka 5, SPRAVOCHNO,
                  ishod v skrin ne vhodit, paru NE invalidiruet).

usage:
  python collect_window_v1.py run     --data-dir .\\data   # edinstvennyy progon po oknu sec.7b
  python collect_window_v1.py dryrun  --data-dir .\\data   # suhoy progon 02-01..02-07 (ne v reshenie)
  python collect_window_v1.py enum    --data-dir .\\data   # tolko enumeraciya + sostav tirov
  python collect_window_v1.py repull        --data-dir .\\data                 # POPRAVKA12 Dif2: sbor syryh /trades -> trades_raw_win/ (RUN po slovu)
  python collect_window_v1.py verify-passa  --data-dir .\\data                 # POPRAVKA12: sverka Prohoda A iz raw_win s frozen (1e-9 + sum n_trades)
  python collect_window_v1.py cascade-probe --data-dir .\\data                 # POPRAVKA12 (AM1): count-only kaskad P11
  python collect_window_v1.py run     --data-dir .\\data --p11 --source=raw_win # Prohod B (pereschyot) iz re-pull dannyh
  python collect_window_v1.py selftest
"""
import json, os, re, sys, time, math, threading, collections, tempfile
import urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

# ------------------------------- konstanty -------------------------------
GAMMA = os.environ.get("PM_GAMMA_HOST", "https://gamma-api.polymarket.com")
DATA  = os.environ.get("PM_DATA_HOST",  "https://data-api.polymarket.com")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

# --- granicy okna (RESHENIE 1, podtverzhdeno): poluotkrytyy interval [start,end_excl) ---
#   Pravaya granica ISKLYUCHAET rezhim CLOB V2 (V2 s 2026-04-28): gst < WIN_END_EXCL.
WIN_START     = "2026-02-01"
WIN_END_EXCL  = "2026-04-28"
V2_LIVE_FROM  = "2026-04-28"
# PRAVKA 1: enum end_date span s bolshim buferom (endDate >= gameStartTime; otbor strogo po gst)
ENUM_END_MIN  = "2026-01-01"
ENUM_END_MAX  = "2026-06-02"
# KONTROL: ATP+WTA v okne dolzhno byt' ~4068
# POPRAVKA 8 (vetka 3): 1 rynok okna isklyuchaetsya po nedokazuemosti predmatch-polnoty ->
#   diapazon smeshchyon na 1 vniz: 4050..4110 -> 4049..4109 (proveryaetsya POSLE isklyucheniya).
CONTROL_LO, CONTROL_HI, CONTROL_REF = 4049, 4109, 4068
MAX_EXCLUDED = 20          # POPRAVKA 8: esli isklyuchennyh > 20 -> pravilo ne kraevoe -> PADAET (peresmotr)

SINGLES_RE = re.compile(r"-(\d{4})-(\d{2})-(\d{2})$")   # maska daty v slage
TIERS = ("atp", "wta", "itf")
DECISION_TIERS = ("atp", "wta")

MIN_MATCHES = 100          # sec.5: >=100 (matchey dlya tira / deystvitelnyh par dlya koshelka)
MIN_DAYS = 60              # sec.5: >=60 dney razmah
ETALON_MAX_AGE = 3600      # sec.2: etalon <= 60 minut do starta
WINNER_HI, WINNER_LO = 0.9, 0.1     # terminalnyy pobeditel (verbatim diag_filter7_degeneracy)
TERM_HI, TERM_LO = 0.999, 0.001     # DOBAVKA3: terminalnye (raschyotnye) ceny, ne rynochnye
# ---- POPRAVKA 11 (storona vhoda): terminalnye sdelki ne vhodyat v entry_vwap ----
# Predikat = is_term_price na SYROY cene (TOZHDESTVENNO DOBAVKA3 :471). n_trades zamorozhen.
DROP_REASON_P11 = "p11_terminal_entry_empty"   # OTDELNOE novoe znachenie, sushchestvuyushchie NE pereispolzuem
EXPECTED_DROP_TO_ZERO = 18                       # offline-srez: par s pustym vhodom rovno 18 (PROVERYAEMO -> gate)

# ---- POPRAVKA 12 (Dif 2): re-pull + harness Prohoda A + kaskad-proba ----
REPULL_DIR_NAME   = "trades_raw_win"     # OTDELNAYA direktoriya; staraya trades_raw NE trogaetsya
REPULL_MANIFEST   = "manifest.jsonl"     # manifest na rynok (odna stroka na rynok; resume-friendly)
REPULL_LOCK       = ".repull.lock"       # guard p.6: ne zapuskat' parallelno s kollektorom (DuckDB 1-pisatel)
REPULL_BACKOFF    = (1, 2, 4, 8, 16)     # POPRAVKA12: 5 povtorov, sekundy (NE frozen 0.5..8 iz get())
PASSA_TOL         = 1e-9                  # dopusk sverki chislovyh poley Prohoda A s frozen
FROZEN_JSON_DEFAULT    = "collect_window_2026-02-01_2026-04-28.json"
FROZEN_PARQUET_DEFAULT = "collect_window_2026-02-01_2026-04-28_pairs.parquet"

OFFSET_CAP = 2000          # gamma deep-offset cap; popadanie -> PADAET
TRADES_LIMIT = 10000       # /trades limit za otvet (proven recover3d)
TRADES_OFFMAX = 10000      # /trades offset: pervyy progon dal HTTP 400 na offset=11000 (market-endpoint)
                            # -> offset>10000 endpoint NE prinimaet i dlya /trades?market=; dostizhimo <=20000 sdelok/rynok
OBSERVED_MAX = 10500       # DOBAVKA 03.08: nablyudyonnyy probe1 maksimum sdelok/rynok v okne (Feb=10500)
HARD_CAP = 2 * TRADES_LIMIT  # 20000: granica razumnogo (NE API-potolok); rynok >= HARD_CAP -> predmatch-hvost ne dokazan
TRANSIENT = (408, 429, 500, 502, 503, 504)   # PRAVKA2: tolko eti retry-yatsya
MAX_RETRIES = 5            # PRAVKA2: do 5 povtorov na tranzientnyy status
REQ_TIMEOUT = 90.0         # bolshoy limit=10000 -> bolshoy otvet
MAX_PER_10S = 140          # obshchiy throttle: 140 zaprosov / 10 s (server-limit 200)
WORKERS = 6


class AmbiguousInput(Exception):
    """Neodnoznachnyy vhod -> fail-fast (sec.3 / trebovanie odnogo progona)."""


# ---------- okno kak izmenyaemoe sostoyanie (dlya rezhima dryrun) ----------
def dparse(s):
    return datetime(int(s[:4]), int(s[5:7]), int(s[8:10]), tzinfo=timezone.utc)

def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

WIN_A = int(dparse(WIN_START).timestamp())
WIN_B = int(dparse(WIN_END_EXCL).timestamp())

def set_window(start, end_excl):
    global WIN_START, WIN_END_EXCL, WIN_A, WIN_B
    WIN_START = start; WIN_END_EXCL = end_excl
    WIN_A = int(dparse(start).timestamp())
    WIN_B = int(dparse(end_excl).timestamp())


# ------------------------------- throttle --------------------------------
_win = collections.deque()
_lock = threading.Lock()

def throttle():
    while True:
        with _lock:
            now = time.time()
            while _win and now - _win[0] > 10.0:
                _win.popleft()
            if len(_win) < MAX_PER_10S:
                _win.append(now); return
            wait = 10.0 - (now - _win[0]) + 0.02
        time.sleep(max(0.0, wait))


# ---------- HTTP s retry-klassifikaciey (PRAVKA2) ----------
_ret_lock = threading.Lock()
_retries_ok = [0]     # summarnoe chislo uspeshnyh retraev (zapros doshel posle >=1 povtora)

def get(url):
    """Vozvrat: dannye (list/dict) libo {'__error__':...}. Tranzientnye statusy -> do 5
    povtorov s narastayushchey pauzoy. Vsyo ostalnoe -> oshibka srazu (bez retry)."""
    throttle()
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    used = 0
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
                if used:
                    with _ret_lock:
                        _retries_ok[0] += used
                return data
        except urllib.error.HTTPError as e:
            if e.code in TRANSIENT and attempt < MAX_RETRIES:
                used += 1
                time.sleep(min(0.5 * (2 ** attempt), 8.0))   # narastayushchaya pauza
                throttle()
                continue
            return {"__error__": "HTTP %d %s" % (e.code, e.reason),
                    "__http__": e.code,
                    "__exhausted__": (e.code in TRANSIENT)}
        except urllib.error.URLError as e:
            return {"__error__": "conn: %s" % (e.reason,), "__conn__": True}
        except Exception as e:
            return {"__error__": "%s: %s" % (type(e).__name__, e)}
    return {"__error__": "exhausted transient", "__exhausted__": True}


def get_or_die(url, what):
    r = get(url)
    if isinstance(r, dict) and r.get("__error__"):
        if r.get("__exhausted__"):
            raise AmbiguousInput("ischerpany %d povtorov (%s): %s -> %s"
                                 % (MAX_RETRIES, what, url, r["__error__"]))
        raise AmbiguousInput("zapros ne udalsya, padenie srazu (%s): %s -> %s"
                             % (what, url, r["__error__"]))
    return r


# ------------------------------- time / parse ----------------------------
def parse_ts(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None

def gst_in_window(gst):
    return gst is not None and WIN_A <= gst < WIN_B

def quantile(sorted_vals, q):
    """Lineynaya interpolyaciya po otsortirovannomu spisku. None esli pusto."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_vals[lo])
    frac = pos - lo
    return float(sorted_vals[lo]) * (1 - frac) + float(sorted_vals[hi]) * frac

def is_term_price(p):
    return (p is not None) and (p >= TERM_HI or p <= TERM_LO)

def iso_ts(ts):
    if ts is None:
        return "-"
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


# ------------------------------- slug / tier -----------------------------
def tier_of(slug):
    s = (slug or "").lower()
    if "-doubles-" in s or s.endswith("-doubles"):
        return None
    for t in TIERS:
        if s.startswith(t + "-"):
            return t
    return None

def slug_date(slug):
    m = SINGLES_RE.search(slug or "")
    if not m:
        return None
    return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))

def is_singles(slug):
    return slug_date(slug) is not None and "doubles" not in (slug or "")

def slug_date_in_window(slug):
    d = slug_date(slug)
    return d is not None and WIN_START <= d < WIN_END_EXCL


# ------------------------------- enumeraciya -----------------------------
def fetch_events(closed, start_dt, end_dt):
    events = {}
    cur = start_dt
    while cur < end_dt:
        s = cur; e = min(cur + timedelta(days=7), end_dt); offset = 0
        while offset < OFFSET_CAP:
            qs = urllib.parse.urlencode({
                "tag_slug": "tennis", "closed": str(closed).lower(),
                "end_date_min": iso(s), "end_date_max": iso(e),
                "limit": 100, "offset": offset, "order": "endDate", "ascending": "true"})
            r = get_or_die(GAMMA + "/events?" + qs, "gamma events")
            if not isinstance(r, list) or not r:
                break
            for ev in r:
                if isinstance(ev, dict) and ev.get("id") is not None:
                    events[ev["id"]] = ev
            if len(r) < 100:
                break
            offset += 100
        else:
            raise AmbiguousInput(
                "gamma offset cap %d na slice %s..%s (closed=%s): vozmozhno molchalivoe "
                "usechenie sobytiy." % (OFFSET_CAP, iso(s), iso(e), closed))
        cur = e
    return list(events.values())

def parse_clob(m):
    ctok = m.get("clobTokenIds")
    if isinstance(ctok, str):
        try:
            ctok = json.loads(ctok)
        except Exception:
            ctok = None
    if not isinstance(ctok, list) or len(ctok) != 2:
        raise AmbiguousInput(
            "clobTokenIds ne rovno 2 (slug=%s): %r -- token T* ne ugadyvaetsya (sec.3)"
            % (m.get("slug"), m.get("clobTokenIds")))
    a, b = str(ctok[0]), str(ctok[1])
    if not a or not b or a == b:
        raise AmbiguousInput("clobTokenIds vyrozhden (slug=%s): %r" % (m.get("slug"), ctok))
    return [a, b]

def flatten(events):
    markets = {}
    for ev in events:
        for m in (ev.get("markets") or []):
            slug = (m.get("slug") or "")
            cond = m.get("conditionId") or m.get("condition_id")
            if not cond or not is_singles(slug):
                continue
            tier = tier_of(slug)
            if tier is None:
                continue
            clob = parse_clob(m)
            gst = parse_ts(m.get("gameStartTime"))
            rec = {"cond": cond, "slug": slug, "tier": tier, "gst": gst,
                   "clob": clob, "event_id": ev.get("id")}
            prev = markets.get(cond)
            if prev is not None:
                if (prev["tier"] != tier or prev["gst"] != gst
                        or prev["clob"] != clob or prev["slug"] != slug):
                    raise AmbiguousInput("konflikt metadannyh cond=%s: %r vs %r" % (cond, prev, rec))
                continue
            markets[cond] = rec
    return markets

def enumerate_window(enum_min, enum_max):
    print("[enum] gamma tag_slug=tennis, end_date %s..%s, oba closed sostoyaniya" % (enum_min, enum_max))
    evs = {}
    for closed in (True, False):
        got = fetch_events(closed, dparse(enum_min), dparse(enum_max))
        for ev in got:
            if ev.get("id") is not None:
                evs[ev["id"]] = ev
        print("  closed=%s: sobytiy nakopleno=%d" % (closed, len(evs)))
    markets = flatten(list(evs.values()))
    print("[enum] singles ATP/WTA/ITF rynkov vsego (do okna): %d" % len(markets))
    return markets


# ------------------------------- sostav tirov ----------------------------
def window_composition(markets):
    comp = {t: {"slug_in_win": 0, "gst_in_win": 0, "gst_missing": 0, "gst_out": 0} for t in TIERS}
    gsts = {t: [] for t in TIERS}
    in_window = {}
    for cond, m in markets.items():
        t = m["tier"]
        if not slug_date_in_window(m["slug"]):
            continue
        comp[t]["slug_in_win"] += 1
        if m["gst"] is None:
            comp[t]["gst_missing"] += 1          # sec.7d stroka 2 (uroven rynka)
        elif gst_in_window(m["gst"]):
            comp[t]["gst_in_win"] += 1
            gsts[t].append(m["gst"])
            in_window[cond] = m
        else:
            comp[t]["gst_out"] += 1              # slug v okne, gst vne (rassoglasovanie)
    return in_window, comp, gsts

def tier_admission(matches, gst_list):
    """DOPUSK TIRA (sec.5, do koshelkov): >=100 matchey v okne I razmah >=60 dney."""
    span = ((max(gst_list) - min(gst_list)) / 86400.0) if len(gst_list) >= 2 else 0.0
    passes = (matches >= MIN_MATCHES) and (span >= MIN_DAYS)
    return span, passes


# ------------------------------- /trades ---------------------------------
def pull_trades(cond):
    """Vse dostizhimye sdelki rynka: limit=10000, offset {0,10000} (proven recover3d).
    Vozvrat (rows, complete_bool). complete=True TOLKO esli doshli do istinnogo nachala
    istorii (poslednyaya stranica < limit). Inache hvost samyh STARYH (=predmatch) sdelok
    nedostizhim -> vyzyvayushchiy kod pometit rynok kak nepolnyy (fail-fast v konce).
    Oshibka zaprosa -> raise (get_or_die)."""
    rows = []
    for off in (0, TRADES_OFFMAX):
        qs = urllib.parse.urlencode({"market": cond, "limit": TRADES_LIMIT, "offset": off})
        r = get_or_die(DATA + "/trades?" + qs, "data-api trades cond=%s off=%d" % (cond, off))
        if not isinstance(r, list):
            raise AmbiguousInput("trades: otvet ne spisok cond=%s off=%d" % (cond, off))
        rows.extend(r)
        if len(r) < TRADES_LIMIT:
            return rows, True          # doshli do nachala istorii -> polno
        # stranica polnaya -> est' escho starshe; probuem sleduyushchee smeshchenie
    # obe stranicy polnye (>=20000): hvost staryh sdelok za predelom offset-cap
    return rows, False


def idx_of(t, clob_list, clob_set):
    asset = str(t.get("asset") or "")
    if asset and asset in clob_set:
        return clob_list.index(asset)
    oi = t.get("outcomeIndex")
    if oi in (0, 1):
        return int(oi)
    if oi in ("0", "1"):
        return int(oi)
    raise AmbiguousInput("ne opredelit' outcome index: asset=%r outcomeIndex=%r cond=%s"
                         % (asset, oi, t.get("conditionId")))

def _num(v, name, cond):
    try:
        return float(v)
    except Exception:
        raise AmbiguousInput("ne chislovoe pole %s=%r cond=%s" % (name, v, cond))


def convolve(prematch_trades, clob_list, clob_set, cond, p11=False):
    """sec.3 svyortka predmatch sdelok koshelka v odnu chistuyu poziciyu.
    (N, entry_vwap, direction, two_sided, n_used). direction: +1 long T*, -1 short T*, 0 pri N=0.
    POPRAVKA 11 (p11=True): terminalnye sdelki (is_term_price na SYROY cene -- TOZHDESTVENNO DOBAVKA3
    :471) NE vhodyat v entry_vwap. N/direction/two_sided schitayutsya po VSEM sdelkam (n_trades zamorozhen).
    n_used = chislo sdelok storony napravleniya, realno voshedshih v entry posle isklyucheniya.
    Esli napravlenie est' (N!=0), no n_used==0 -> entry=None BEZ isklyucheniya AmbiguousInput -> DROP_REASON_P11.
    Pri p11=False povedenie POBITOVO zamorozhennoe (Prohod A)."""
    signed = 0.0
    contribs = []
    has_pos = has_neg = False
    for t in prematch_trades:
        idx = idx_of(t, clob_list, clob_set)
        price = _num(t.get("price"), "price", cond)
        size = _num(t.get("size"), "size", cond)
        # POPRAVKA 10: proverka na SYROM price, DO konversii komplementa (1-p). Porchennye
        # sdelki (price vne [0,1]) otseyany vyshe v filter_bad_prices; zdes' guard-backstop.
        if not (0.0 <= price <= 1.0):
            raise AmbiguousInput("cena vne [0,1]: %r cond=%s" % (price, cond))
        if size <= 0.0:
            raise AmbiguousInput("razmer <=0: %r cond=%s" % (size, cond))
        side = (t.get("side") or "").upper()
        if side not in ("BUY", "SELL"):
            raise AmbiguousInput("neizvestnaya storona side=%r cond=%s" % (t.get("side"), cond))
        term = is_term_price(price)   # POPRAVKA 11: SYRAYA cena, TA ZHE is_term_price chto DOBAVKA3 (:471)
        if idx == 0:
            tstar_price = price
            base_dir = 1 if side == "BUY" else -1
        else:
            tstar_price = 1.0 - price
            base_dir = -1 if side == "BUY" else 1
        signed += base_dir * size
        contribs.append((base_dir, size, tstar_price, term))
        if base_dir > 0: has_pos = True
        else: has_neg = True
    N = signed
    if abs(N) < 1e-9:
        return 0.0, None, 0, (has_pos and has_neg), 0
    direction = 1 if N > 0 else -1
    num = den = 0.0
    used = 0
    for base_dir, size, tstar_price, term in contribs:
        if base_dir == direction:
            if p11 and term:
                continue              # POPRAVKA 11: terminalnaya sdelka storony -> NE v entry_vwap
            num += size * tstar_price
            den += size
            used += 1
    if p11 and den <= 0.0:
        # napravlenie est' (N!=0), no vse sdelki storony terminalny -> vhod pust -> DROP_REASON_P11
        return N, None, direction, (has_pos and has_neg), 0
    entry = (num / den) if den > 0 else None
    if entry is None:
        raise AmbiguousInput("pustaya storona chistoy pozicii cond=%s (N=%r)" % (cond, N))
    return N, entry, direction, (has_pos and has_neg), used


def market_etalon(all_trades, gst, clob_list, clob_set, cond):
    """sec.2 etalon: poslednyaya RYNOCHNAYA sdelka strogo ranshe gst, cherez komplement.
    DOBAVKA3: sdelki po terminalnym cenam (>=0.999 / <=0.001) IZ VYBORA ISKLYUCHAYUTSYA.
    Vozvrat (line0, age_sec)."""
    best_ts = None; best = None
    for t in all_trades:
        ts = parse_ts(t.get("timestamp"))
        if ts is None or ts >= gst:
            continue
        price = _num(t.get("price"), "price", cond)
        if is_term_price(price):
            continue                     # raschyotnaya cena -> ne opornaya
        if best_ts is None or ts > best_ts:
            best_ts = ts; best = (t, price)
    if best is None:
        return None, None
    t, price = best
    idx = idx_of(t, clob_list, clob_set)
    if not (0.0 <= price <= 1.0):
        raise AmbiguousInput("etalon cena vne [0,1]: %r cond=%s" % (price, cond))
    line0 = price if idx == 0 else (1.0 - price)
    return line0, (gst - best_ts)


def terminal_winner(all_trades, clob_list, clob_set, cond):
    last = {0: None, 1: None}
    for t in all_trades:
        ts = parse_ts(t.get("timestamp"))
        if ts is None:
            continue
        idx = idx_of(t, clob_list, clob_set)
        price = _num(t.get("price"), "price", cond)
        cur = last[idx]
        if cur is None or ts > cur[0]:
            last[idx] = (ts, price)
    if last[0] is None or last[1] is None:
        return None
    p0, p1 = last[0][1], last[1][1]
    if p0 >= WINNER_HI and p1 <= WINNER_LO:
        return 0
    if p1 >= WINNER_HI and p0 <= WINNER_LO:
        return 1
    return None


def clv_of(direction, entry, p_ref):
    """sec.4: long T* -> p_ref-entry ; short T* -> entry-p_ref."""
    if direction == 1:
        return p_ref - entry
    if direction == -1:
        return entry - p_ref
    return None


# ------------------------------- parquet (DOBAVKA1) ----------------------
PAIR_COLS = ("wallet", "slug", "tier", "gameStartTime", "n_trades",
             "entry_vwap", "p_ref", "p_ref_age_min", "clv", "dropped_reason")
# POPRAVKA 11: v Prohode B dobavlyaetsya kolonka n_trades_used_p11 (srazu POSLE n_trades).
PAIR_COLS_P11 = PAIR_COLS[:5] + ("n_trades_used_p11",) + PAIR_COLS[5:]

def write_pairs_parquet(path, rows, p11=False):
    import pyarrow as pa, pyarrow.parquet as pq
    fields = [
        ("wallet", pa.string()), ("slug", pa.string()), ("tier", pa.string()),
        ("gameStartTime", pa.int64()), ("n_trades", pa.int64())]
    if p11:
        fields.append(("n_trades_used_p11", pa.int64()))   # POPRAVKA 11: srazu posle n_trades
    fields += [
        ("entry_vwap", pa.float64()), ("p_ref", pa.float64()),
        ("p_ref_age_min", pa.float64()), ("clv", pa.float64()),
        ("dropped_reason", pa.string())]
    schema = pa.schema(fields)   # p11=False: schema POBITOVO kak frozen (from_pylist ignoriruet lishnie klyuchi)
    tmp = path + ".tmp"
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), tmp)
    os.replace(tmp, path)


# ------------------------------- POPRAVKA 10: porchennye ceny ------------
def filter_bad_prices(rows, gst, cond, slug):
    """POPRAVKA 10 (§3): otseivaet sdelki s SYRYM price vne [0,1] -- proverka DO konversii
    komplementa (1-p), inache 1.68 na protivopolozhnom tokene stal by -0.68 i proshel by
    verhnyuyu granicu. Ne klampit, ne interpoliruet. Vozvrat (clean_rows, bad_records);
    bad_records nesut vse polya dlya data/validate/bad_prices.csv + pre_match."""
    clean = []
    bad = []
    for t in rows:
        v = t.get("price")
        try:
            p = float(v)
        except (TypeError, ValueError):
            clean.append(t)          # nechislovoy price -- otdelnaya neodnoznachnost', ne pravilo P10
            continue
        if p < 0.0 or p > 1.0:
            ts = parse_ts(t.get("timestamp"))
            bad.append({
                "conditionId": cond,
                "slug": slug,
                "proxyWallet": (t.get("proxyWallet") or ""),
                "side": (t.get("side") or ""),
                "size": t.get("size"),
                "price": p,
                "timestamp": (ts if ts is not None else t.get("timestamp")),
                "outcomeIndex": t.get("outcomeIndex"),
                "transactionHash": (t.get("transactionHash") or ""),
                "pre_match": (1 if (ts is not None and gst is not None and ts < gst) else 0),
            })
        else:
            clean.append(t)
    return clean, bad


def _write_bad_prices_csv(data_dir, records):
    """POPRAVKA 10 (§4): otchyot data/validate/bad_prices.csv. Pishetsya vsegda (dazhe pustoy,
    tolko zagolovok) i pered padeniem predohranitelya (forensika)."""
    import csv
    vdir = os.path.join(data_dir, "validate")
    if not os.path.isdir(vdir):
        os.makedirs(vdir)
    path = os.path.join(vdir, "bad_prices.csv")
    cols = ["conditionId", "slug", "proxyWallet", "side", "size", "price",
            "timestamp", "outcomeIndex", "transactionHash", "pre_match"]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=cols)
        wr.writeheader()
        for r in records:
            wr.writerow({k: r.get(k) for k in cols})
    os.replace(tmp, path)
    return path


# ------------------------------- sbor + voronka --------------------------
def collect(data_dir, enum_min, enum_max, do_control, dry, p11=False, source="network", source_dir=None):
    if not os.path.isdir(data_dir):
        os.makedirs(data_dir)
    tag = "DRYRUN " if dry else ""
    markets_all = enumerate_window(enum_min, enum_max)
    in_window, comp, gsts = window_composition(markets_all)

    # ---- PREFLIGHT + ISKLYUCHENIE (POPRAVKA 8, vetka 3): DO sostava/kontrolya/voronki projti po
    #      VSEM ATP/WTA rynkam okna pervoy(-ymi) stranicami /trades, vzyat' chislo sdelok. Rynok s
    #      NEDOKAZUEMOY predmatch-polnotoy (obe stranicy polny -> dostizhimo rovno 20000 samyh
    #      svezhih sdelok, starshe -- za stenoy offset) ISKLYUCHAETSYA iz okna DO lyubogo CLV i
    #      nezavisimo ot rezultatov. Ustanovleno probnikom 03.08: limit nasyshchaetsya na 10000 pri
    #      lyubom znachenii (20000/30000/50000 -> rovno 10000); offset>10000 -> HTTP 400; sortirovka
    #      po vozrastaniyu ne podderzhivaetsya (6 variantov parametra proignorirovany, lenta vsegda
    #      DESC). PADENIE ostayotsya TOLKO esli isklyuchennyh > MAX_EXCLUDED (pravilo perestayot byt'
    #      kraevym). Voronka nizhe pereispolzuet uzhe skachannye trades bez povtora.
    targets = [m for m in in_window.values() if m["tier"] in DECISION_TIERS]
    print("\n===== %sPREFLIGHT /trades: chislo sdelok po rynkam (DO sostava/otbora) =====" % tag)
    print("[preflight] rynkov ATP/WTA v okne: %d (limit=%d, offset<=%d, throttle %d/10s, workers=%d)"
          % (len(targets), TRADES_LIMIT, TRADES_OFFMAX, MAX_PER_10S, WORKERS))
    print("[preflight] HARD_CAP (predmatch nedokazuem) = %d sdelok; WARNING-porog (nabl. maksimum) = %d"
          % (HARD_CAP, OBSERVED_MAX))
    if not targets:
        raise AmbiguousInput("net ni odnogo ATP/WTA rynka s gst v okne -- proveryat' okno/enumeraciyu")

    trades_by_cond = {}
    counts = {}                     # cond -> chislo sdelok (tochnoe, esli complete)
    excluded = []                   # POPRAVKA 8: rynki s nedokazuemoy predmatch-polnotoy -> ISKLYUCHENIE
    warns = []                      # rynki glubzhe OBSERVED_MAX (vklyuchaya isklyuchennye)
    max_count = 0; max_slug = None
    t0 = time.time(); done = 0
    # POPRAVKA12: source='network' -- kak frozen (pull_trades po seti); source='raw_win' -- chtenie s diska re-pull.
    _src_dir = source_dir or data_dir
    def _acquire(m):
        if source == "raw_win":
            return read_trades_raw_win(_src_dir, m["cond"])   # (rows, complete) iz trades_raw_win/ (manifest)
        return pull_trades(m["cond"])
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_acquire, m): m for m in targets}
        for fut in as_completed(futs):
            m = futs[fut]
            rows, complete = fut.result()      # oshibka zaprosa -> vsplyvyot (fail-fast)
            trades_by_cond[m["cond"]] = rows   # sohranyaem: voronka nizhe pereispolzuet, bez povtora
            n = len(rows)
            counts[m["cond"]] = n
            if n > max_count:
                max_count = n; max_slug = m["slug"]
            if not complete:
                oldest_ts = parse_ts(rows[-1].get("timestamp")) if rows else None
                reach_h = ((m["gst"] - oldest_ts) / 3600.0) if (m["gst"] is not None and oldest_ts is not None) else None
                excluded.append({"cond": m["cond"], "slug": m["slug"], "tier": m["tier"],
                                 "gst": m["gst"], "oldest_ts": oldest_ts, "reach_hours": reach_h})
                warns.append((m["cond"], m["slug"], ">=%d" % HARD_CAP))
                print("    WARNING NEPOLNO (>=%d sdelok, predmatch-hvost ne dokazan -> ISKLYUCHENIE): %s  %s"
                      % (HARD_CAP, m["cond"], m["slug"]))
            elif n > OBSERVED_MAX:
                warns.append((m["cond"], m["slug"], str(n)))
                print("    WARNING glubzhe nabl. maksimuma %d: %s  %s  sdelok=%d"
                      % (OBSERVED_MAX, m["cond"], m["slug"], n))
            done += 1
            if done % 200 == 0:
                el = time.time() - t0; rate = done / el if el else 0
                print("    preflight %d/%d rynkov | %.1f/s" % (done, len(targets), rate))

    max_disp = (">=%d" % HARD_CAP) if (excluded and max_count >= HARD_CAP) else str(max_count)
    print("[preflight] gotovo: maks sdelok/rynok = %s (%s); rynkov > %d = %d; nepolnyh (>=%d) = %d"
          % (max_disp, max_slug, OBSERVED_MAX, len(warns), HARD_CAP, len(excluded)))

    # PADENIE TOLKO esli isklyuchennyh > MAX_EXCLUDED: pravilo perestayot byt' kraevym -> peresmotr.
    if len(excluded) > MAX_EXCLUDED:
        head = "\n".join("    %s  %s  sdelok>=%d" % (e["cond"], e["slug"], HARD_CAP) for e in excluded[:30])
        raise AmbiguousInput(
            "SLISHKOM MNOGO NEPOLNYH RYNKOV: %d > MAX_EXCLUDED=%d. Isklyuchenie po nedokazuemosti "
            "predmatch-polnoty perestalo byt' kraevym -> POPRAVKA 8 trebuet peresmotra, ostanovka "
            "DO sbora. Spisok (do 30):\n%s" % (len(excluded), MAX_EXCLUDED, head))

    # ISKLYUCHENIE iz okna DO sostava/kontrolya/voronki (nezavisimo ot rezultatov):
    excluded_conds = set(e["cond"] for e in excluded)
    for e in excluded:
        c = e["cond"]; t = e["tier"]
        if c in in_window:
            del in_window[c]
        if comp[t]["gst_in_win"] > 0:
            comp[t]["gst_in_win"] -= 1
        g = e["gst"]
        if g is not None and g in gsts[t]:
            gsts[t].remove(g)
    targets = [m for m in targets if m["cond"] not in excluded_conds]
    if excluded:
        print("\n[isklyuchenie] POPRAVKA 8: iz okna udaleno %d rynkov (nedokazuemaya predmatch-polnota):"
              % len(excluded))
        for e in excluded:
            print("    %s  %s  gst=%s  starishaya_dostizhimaya=%s  raznica=%s ch (dostizhimo predmatch)"
                  % (e["cond"], e["slug"], iso_ts(e["gst"]), iso_ts(e["oldest_ts"]),
                     ("%.2f" % e["reach_hours"]) if e["reach_hours"] is not None else "n/d"))
    if not targets:
        raise AmbiguousInput("posle isklyucheniya (POPRAVKA 8) ne ostalos' ATP/WTA rynkov okna")

    print("\n===== %sSTROKA SOSTAVA TIROV V OKNE (DO otbora koshelkov, POSLE isklyucheniya POPRAVKA 8) =====" % tag)
    print("okno po gameStartTime: [%s 00:00Z, %s 00:00Z)  (pravaya granica isklyuchaet CLOB V2 s %s)"
          % (WIN_START, WIN_END_EXCL, V2_LIVE_FROM))
    for t in TIERS:
        c = comp[t]
        flag = "" if t in DECISION_TIERS else "  [issledovatelskiy]"
        print("  %-3s: v okne(gst)=%-5d | slug_v_okne=%-5d gst_net=%-4d gst_vne=%-4d%s"
              % (t.upper(), c["gst_in_win"], c["slug_in_win"], c["gst_missing"], c["gst_out"], flag))
    itf_n = comp["itf"]["gst_in_win"]
    print("  ITF = %d v okne -> issledovatelskiy tir %s (sootv. sec.5/7b)."
          % (itf_n, "NE zapuskaetsya" if itf_n == 0 else "PROVERIT'"))

    # KONTROL 4068 (tolko real'nyy progon)
    atp_wta = comp["atp"]["gst_in_win"] + comp["wta"]["gst_in_win"]
    print("\n[control] ATP+WTA v okne (gst) = %d (etalon polnogo perebora ~%d, dopustimo %d..%d)"
          % (atp_wta, CONTROL_REF, CONTROL_LO, CONTROL_HI))
    if do_control and not (CONTROL_LO <= atp_wta <= CONTROL_HI):
        raise AmbiguousInput(
            "KONTROL PROVALEN: ATP+WTA v okne = %d vne [%d..%d]. Zapas po end_date podobran "
            "neverno -> ostanovka DO sbora." % (atp_wta, CONTROL_LO, CONTROL_HI))
    elif not do_control:
        print("[control] dryrun: kontrol 4068 NE primenyaetsya (okno drugoe).")

    # DOPUSK TIRA (do koshelkov) + verdikt sec.7e
    print("\n===== %sDOPUSK TIRA (sec.5, DO otbora koshelkov: >=%d matchey I >=%d dney) ====="
          % (tag, MIN_MATCHES, MIN_DAYS))
    tier_adm = {}
    window_unusable = False
    for t in DECISION_TIERS:
        span, passes = tier_admission(comp[t]["gst_in_win"], gsts[t])
        tier_adm[t] = {"matches": comp[t]["gst_in_win"], "span_days": round(span, 2), "passes": passes}
        print("  %-3s: matchey=%-5d razmah=%.1f dney -> %s"
              % (t.upper(), comp[t]["gst_in_win"], span, "PROSHEL" if passes else "NE PROSHEL"))
        if not passes:
            window_unusable = True

    # (PREFLIGHT + ISKLYUCHENIE POPRAVKA 8 vypolneny vyshe, srazu posle window_composition:
    #  targets/trades_by_cond/warns/max_count/excluded uzhe gotovy, isklyuchennye rynki udaleny.)

    # ---- POPRAVKA 10: validaciya syryh cen /trades (DO konversii komplementa, DO voronki) ----
    #  Sdelki s price vne [0,1] otbrasyvayutsya (ne klamp/interpol), pishutsya v CSV i
    #  primenyayutsya edinoobrazno k entry (convolve) I k etalonu p_ref (§2 P7). Predohraniteli
    #  (zadany do progona, izmeneniyu ne podlezhat): po rynku dolya >1.0% I >=10 v absolyute
    #  -> padenie; po progonu >200 -> padenie. Prodolzhat' posle srabotki predohranitelya nelzya.
    BADPRICE_MARKET_SHARE = 0.01     # >1.0%
    BADPRICE_MARKET_MINABS = 10      # I >=10 v absolyute
    BADPRICE_RUN_MAX = 200           # po vsemu progonu
    clean_by_cond = {}
    bad_price_records = []           # vse porchennye sdelki (CSV + otchyot)
    etalon_corrupt = []              # matchi, gde porchenaya sdelka byla by posledney pered gst
    badprice_lost_units = 0          # M: edinicy koshelyok-match, ischeznuvshie iz-za drop
    for m in targets:
        cond = m["cond"]; gst = m["gst"]; slug = m["slug"]
        rows = trades_by_cond.get(cond, [])
        clean, bad = filter_bad_prices(rows, gst, cond, slug)
        clean_by_cond[cond] = clean
        if not bad:
            continue
        bad_price_records.extend(bad)
        # --- PREDOHRANITEL' po rynku (proverka SRAZU, do prodolzheniya) ---
        total = len(rows)
        share = (len(bad) / total) if total else 0.0
        if len(bad) >= BADPRICE_MARKET_MINABS and share > BADPRICE_MARKET_SHARE:
            _write_bad_prices_csv(data_dir, bad_price_records)
            raise AmbiguousInput(
                "POPRAVKA 10 predohranitel' RYNKA: %s (%s) -- porchennyh cen %d iz %d (%.3f%%), "
                "prevysheny %.1f%% I >=%d -> ostanovka" %
                (cond, slug, len(bad), total, share * 100.0,
                 BADPRICE_MARKET_SHARE * 100.0, BADPRICE_MARKET_MINABS))
        # --- (§3 п.1) match, gde porchenaya sdelka OKAZALAS' posledney pered gst (isportila by CLV) ---
        latest_pre = None
        for t in rows:
            ts = parse_ts(t.get("timestamp"))
            if ts is not None and gst is not None and ts < gst:
                if latest_pre is None or ts > latest_pre[0]:
                    latest_pre = (ts, t)
        if latest_pre is not None:
            try:
                lp = float(latest_pre[1].get("price"))
            except (TypeError, ValueError):
                lp = None
            if lp is not None and (lp < 0.0 or lp > 1.0):
                etalon_corrupt.append({"cond": cond, "slug": slug, "gst": gst,
                                       "price": lp, "timestamp": latest_pre[0]})
        # --- (§3 п.3) M: koshelyok-match edinicy, poteryannye iz-za drop predmatch porchennyh ---
        raw_pre = collections.defaultdict(int)
        clean_pre = collections.defaultdict(int)
        for t in rows:
            ts = parse_ts(t.get("timestamp"))
            if ts is not None and gst is not None and ts < gst:
                raw_pre[(t.get("proxyWallet") or "").lower()] += 1
        for t in clean:
            ts = parse_ts(t.get("timestamp"))
            if ts is not None and gst is not None and ts < gst:
                clean_pre[(t.get("proxyWallet") or "").lower()] += 1
        for w, c in raw_pre.items():
            if c >= 1 and clean_pre.get(w, 0) == 0:
                badprice_lost_units += 1
    # --- PREDOHRANITEL' po vsemu progonu ---
    if len(bad_price_records) > BADPRICE_RUN_MAX:
        _write_bad_prices_csv(data_dir, bad_price_records)
        raise AmbiguousInput("POPRAVKA 10 predohranitel' PROGONA: porchennyh cen vsego %d > %d -> ostanovka"
                             % (len(bad_price_records), BADPRICE_RUN_MAX))

    # ---- svyortka + voronka po param (koshelyok x match), po tiram ----
    funnel = {t: {
        "markets_in_win": 0,
        "cand_wallets": set(),
        "pairs_total": 0,
        "row1_no_prematch": 0,
        "row3_no_etalon60": 0,
        "row4_net_zero": 0,
        "row5_amb_terminal": 0,
        "row6_p11_entry_empty": 0,
        "valid_pairs": 0,
        "two_sided": 0,
        "wallets_with_prematch": set(),
        "valid_by_wallet": collections.defaultdict(list),
    } for t in DECISION_TIERS}

    term_pre_total = 0            # DOBAVKA3: term-cen vnutri predmatch okna (ozhidaetsya 0)
    p11_drop_total = 0            # POPRAVKA 11: par s pustym vhodom (n_trades_used_p11==0, N!=0)
    pair_rows = []                # DOBAVKA1: KAZHDAYA para koshelyok x match
    valid_json = []               # deystvitelnye pary v JSON-otchyot

    for m in targets:
        cond = m["cond"]; tier = m["tier"]; gst = m["gst"]; clob = m["clob"]
        clob_set = set(clob)
        rows = clean_by_cond.get(cond, [])   # POPRAVKA 10: uzhe bez porchennyh cen (entry I etalon)
        F = funnel[tier]
        F["markets_in_win"] += 1
        line0, et_age = market_etalon(rows, gst, clob, clob_set, cond)
        etalon_ok = (line0 is not None and et_age is not None and et_age <= ETALON_MAX_AGE)
        p_ref_age_min = (round(et_age / 60.0, 3) if et_age is not None else None)
        winner = terminal_winner(rows, clob, clob_set, cond)
        amb_terminal = (winner is None)

        by_wallet = collections.defaultdict(list)
        for t in rows:
            w = (t.get("proxyWallet") or "").lower()
            if not w:
                raise AmbiguousInput("pustoy proxyWallet v sdelke cond=%s" % cond)
            by_wallet[w].append(t)

        for w, wtrades in by_wallet.items():
            F["cand_wallets"].add(w)
            F["pairs_total"] += 1
            prematch = []
            for t in wtrades:
                ts = parse_ts(t.get("timestamp"))
                if ts is not None and ts < gst:
                    prematch.append(t)
                    pr = _num(t.get("price"), "price", cond)
                    if is_term_price(pr):
                        term_pre_total += 1
            n_trades = len(prematch)

            def emit(reason, entry, clv, n_used=None):
                pair_rows.append({
                    "wallet": w, "slug": m["slug"], "tier": tier, "gameStartTime": int(gst),
                    "n_trades": int(n_trades),
                    "n_trades_used_p11": (int(n_used) if n_used is not None else None),
                    "entry_vwap": (round(entry, 6) if entry is not None else None),
                    "p_ref": (round(line0, 6) if (etalon_ok and line0 is not None) else None),
                    "p_ref_age_min": (p_ref_age_min if etalon_ok else None),
                    "clv": (round(clv, 6) if clv is not None else None),
                    "dropped_reason": reason})

            if not prematch:
                F["row1_no_prematch"] += 1
                emit("no_prematch", None, None, (0 if p11 else None))
                continue
            F["wallets_with_prematch"].add(w)
            N, entry, direction, two_sided, n_used = convolve(prematch, clob, clob_set, cond, p11=p11)
            if p11 and abs(N) >= 1e-9 and entry is None:
                # POPRAVKA 11: napravlenie est' (N!=0), no vse sdelki storony terminalny -> vhod pust.
                # Schitaetsya DO etalona (etalon-agnostichno, kak offline-srez) -> gate rovno 18.
                # dropped_reason = OTDELNOE novoe znachenie (perekryvaet no_etalon/net_zero dlya etih par).
                F["row6_p11_entry_empty"] += 1
                p11_drop_total += 1
                emit(DROP_REASON_P11, None, None, 0)
                continue
            if not etalon_ok:
                F["row3_no_etalon60"] += 1
                emit("no_etalon_60min", entry, None, (n_used if p11 else None))
                continue
            if abs(N) < 1e-9:
                F["row4_net_zero"] += 1
                emit("net_zero", None, None, (0 if p11 else None))
                continue
            # deystvitelnaya para
            clv = clv_of(direction, entry, line0)
            F["valid_pairs"] += 1
            if two_sided:
                F["two_sided"] += 1
            if amb_terminal:
                F["row5_amb_terminal"] += 1
            F["valid_by_wallet"][w].append({"cond": cond, "gst": gst})
            emit("", entry, clv, (n_used if p11 else None))
            vj = {
                "tier": tier, "wallet": w, "cond": cond, "slug": m["slug"], "gst": gst,
                "N": round(N, 6), "direction": direction, "entry_vwap": round(entry, 6),
                "p_ref": round(line0, 6), "p_ref_age_min": p_ref_age_min,
                "clv": round(clv, 6), "amb_terminal": amb_terminal, "two_sided": two_sided}
            if p11:
                vj["n_trades_used_p11"] = n_used
            valid_json.append(vj)

    # ------------------------------- POPRAVKA 11 GATE -------------------------------
    if p11 and do_control and p11_drop_total != EXPECTED_DROP_TO_ZERO:
        raise AmbiguousInput(
            "POPRAVKA 11 GATE: par s pustym vhodom (n_trades_used_p11==0, N!=0) = %d, ozhidalos' %d -> "
            "STOP, ne podgonyaem (dyra 0.999 raw-vs-tp ILI predikat/okno/sbor). Sm. p11_entry.py."
            % (p11_drop_total, EXPECTED_DROP_TO_ZERO))

    # ------------------------------- otchyot -------------------------------
    report = {
        "mode": "dryrun" if dry else "run",
        "window": {"start": WIN_START, "end_excl": WIN_END_EXCL, "v2_live_from": V2_LIVE_FROM,
                   "selector": "gameStartTime in [start, end_excl)"},
        "enum_end_date": {"min": enum_min, "max": enum_max},
        "control_atp_wta_in_window": atp_wta,
        "control_applied": bool(do_control),
        "retries_succeeded": _retries_ok[0],
        "terminal_price_prematch_count": term_pre_total,
        "excluded_markets_no_prematch_completeness": [   # POPRAVKA 8: granica poteri dlya proverki zadnim chislom
            {"cond": e["cond"], "slug": e["slug"], "tier": e["tier"],
             "gameStartTime": (int(e["gst"]) if e["gst"] is not None else None),
             "gameStartTime_iso": iso_ts(e["gst"]),
             "oldest_reachable_ts": (int(e["oldest_ts"]) if e["oldest_ts"] is not None else None),
             "oldest_reachable_iso": iso_ts(e["oldest_ts"]),
             "reachable_prematch_hours": (round(e["reach_hours"], 3) if e["reach_hours"] is not None else None)}
            for e in excluded],
        "excluded_markets_count": len(excluded),
        "max_excluded": MAX_EXCLUDED,
        "bad_prices_count": len(bad_price_records),
        "bad_prices_lost_wallet_match_units": badprice_lost_units,
        "bad_prices_would_be_etalon_matches": [
            {"cond": e["cond"], "slug": e["slug"],
             "gameStartTime_iso": iso_ts(e["gst"]), "price": e["price"],
             "timestamp_iso": iso_ts(e["timestamp"])}
            for e in etalon_corrupt],
        "bad_prices_thresholds": {"per_market_share": BADPRICE_MARKET_SHARE,
                                  "per_market_min_abs": BADPRICE_MARKET_MINABS,
                                  "run_total_max": BADPRICE_RUN_MAX},
        "bad_prices_csv": os.path.join("validate", "bad_prices.csv"),
        "preflight_max_trades_per_market": max_count,
        "preflight_observed_max": OBSERVED_MAX,
        "preflight_hard_cap": HARD_CAP,
        "preflight_markets_over_observed_max": [{"cond": c, "slug": s, "trades": n} for c, s, n in warns],
        "tier_composition": comp,
        "tier_admission": tier_adm,
        "tiers": {},
    }

    print("\n===== %sVORONKA sec.7d (po tiram; pary koshelyok x match) =====" % tag)
    print("  rynki s NEDOKAZUEMOY predmatch-polnotoy (isklyucheny DO lyubogo CLV, POPRAVKA 8): %d" % len(excluded))
    for e in excluded:
        print("    %s  %s  gst=%s  starishaya_dostizhimaya=%s  dostizhimo_predmatch=%s ch"
              % (e["cond"], e["slug"], iso_ts(e["gst"]), iso_ts(e["oldest_ts"]),
                 ("%.2f" % e["reach_hours"]) if e["reach_hours"] is not None else "n/d"))
    print("  sdelki s cenoy vne [0,1] (otbrosheny na SYROM price, POPRAVKA 10): %d" % len(bad_price_records))
    print("  edinicy koshelyok-match, poteryannye iz-za porchennyh cen: %d" % badprice_lost_units)
    print("  matchi, gde porchenaya cena byla by etalonom p_ref (§2 P7; ozhidanie 0): %d" % len(etalon_corrupt))
    for e in etalon_corrupt:
        print("    %s  %s  gst=%s  price=%r  ts=%s"
              % (e["cond"], e["slug"], iso_ts(e["gst"]), e["price"], iso_ts(e["timestamp"])))
    for t in DECISION_TIERS:
        F = funnel[t]
        counts = sorted(len(v) for v in F["valid_by_wallet"].values())  # pary/koshelyok (tolko >=1)
        wallets_cut_100 = sum(1 for c in counts if c < MIN_MATCHES)
        wallets_ge_100 = sum(1 for c in counts if c >= MIN_MATCHES)
        # DOPUSK KOSHELKA: >=100 par I razmah >=60 dney, vnutri tira
        n_wallet_pass = 0
        for w, pairs in F["valid_by_wallet"].items():
            if len(pairs) < MIN_MATCHES:
                continue
            gv = [p["gst"] for p in pairs]
            if (max(gv) - min(gv)) / 86400.0 >= MIN_DAYS:
                n_wallet_pass += 1
        dist = {"median": quantile(counts, 0.50), "p75": quantile(counts, 0.75),
                "p90": quantile(counts, 0.90), "p99": quantile(counts, 0.99)}
        tinfo = {
            "markets_in_window": F["markets_in_win"],
            "candidate_wallets": len(F["cand_wallets"]),
            "wallets_with_prematch": len(F["wallets_with_prematch"]),
            "pairs_total": F["pairs_total"],
            "drop_row1_no_prematch": F["row1_no_prematch"],
            "drop_row2_no_gst_market_level": comp[t]["gst_missing"],
            "drop_row3_no_etalon_60min": F["row3_no_etalon60"],
            "drop_row4_net_zero": F["row4_net_zero"],
            "overlay_row5_amb_terminal": F["row5_amb_terminal"],
            "valid_pairs": F["valid_pairs"],
            "two_sided_prematch_pairs": F["two_sided"],
            "two_sided_share": (round(F["two_sided"] / F["valid_pairs"], 4) if F["valid_pairs"] else None),
            "wallets_with_valid_pair": len(counts),
            "wallets_cut_by_100": wallets_cut_100,
            "wallets_ge_100_pairs": wallets_ge_100,
            "wallets_pass_dopusk": n_wallet_pass,          # >=100 par I >=60 dney
            "pairs_per_wallet": dist,
            "tier_admission_passes": tier_adm[t]["passes"],
        }
        if p11:
            tinfo["drop_row6_p11_entry_empty"] = F["row6_p11_entry_empty"]
        report["tiers"][t] = tinfo
        print("  --- %s ---" % t.upper())
        print("    dopusk TIRA               : %s (matchey=%d, razmah=%.1f dney)"
              % ("PROSHEL" if tier_adm[t]["passes"] else "NE PROSHEL",
                 tier_adm[t]["matches"], tier_adm[t]["span_days"]))
        print("    kandidaty (koshelkov)     : %d" % tinfo["candidate_wallets"])
        print("    -> s predmatch poziciey   : %d" % tinfo["wallets_with_prematch"])
        print("    voronka par (vsego par=%d):" % tinfo["pairs_total"])
        print("      stroka1 net predmatch sdelok : %d" % tinfo["drop_row1_no_prematch"])
        print("      stroka2 net gameStartTime    : %d (uroven rynka, sm. sostav tirov gst_net)" % tinfo["drop_row2_no_gst_market_level"])
        print("      stroka3 net etalona <=60 min : %d" % tinfo["drop_row3_no_etalon_60min"])
        print("      stroka4 N=0                  : %d" % tinfo["drop_row4_net_zero"])
        print("      stroka5 neodnoznach. termin. : %d (SPRAVOCHNO, paru ne invalidiruet)" % tinfo["overlay_row5_amb_terminal"])
        if p11:
            print("      stroka6 P11 vhod pust        : %d (n_trades_used_p11==0, N!=0 -> %s)"
                  % (F["row6_p11_entry_empty"], DROP_REASON_P11))
        print("      deystvitelnyh par            : %d" % tinfo["valid_pairs"])
        print("      dolya dvuhstoronnih predmatch: %s" % tinfo["two_sided_share"])
        print("    DOPUSK KOSHELKA (>=%d par I >=%d dney, vnutri tira):" % (MIN_MATCHES, MIN_DAYS))
        print("      koshelkov s >=1 deystv. paroy : %d" % tinfo["wallets_with_valid_pair"])
        print("      raspredelenie par/koshelyok   : mediana=%s p75=%s p90=%s p99=%s"
              % (dist["median"], dist["p75"], dist["p90"], dist["p99"]))
        print("      otsekaet porog 100 (par<100)  : %d koshelkov" % tinfo["wallets_cut_by_100"])
        print("      s >=100 par                   : %d ; iz nih proshli dopusk (>=60 dney): %d"
              % (tinfo["wallets_ge_100_pairs"], tinfo["wallets_pass_dopusk"]))

    report["window_unusable_7e"] = window_unusable
    if window_unusable:
        print("\n!!! sec.7e: hotya by odin reshayushchiy tir NE proshel DOPUSK TIRA -> OKNO NEPRIGODNO CELIKOM.")
    else:
        print("\nsec.7e: oba reshayushchih tira (ATP,WTA) proshli dopusk tira v okne.")

    print("\n[retry] uspeshnyh retraev (408/429/5xx, doshli posle povtora): %d" % _retries_ok[0])
    print("[term ] sdelok po terminalnym cenam VNUTRI predmatch okna: %d (ozhidalos' 0)" % term_pre_total)
    if p11:
        print("[p11  ] par s pustym vhodom posle term-isklyucheniya (n_trades_used_p11==0, N!=0): %d (gate=%d)"
              % (p11_drop_total, EXPECTED_DROP_TO_ZERO))

    report["valid_pairs_count"] = len(valid_json)
    report["pair_rows_count"] = len(pair_rows)
    if p11:
        report["p11_applied"] = True
        report["p11_drop_reason"] = DROP_REASON_P11
        report["p11_expected_drop_to_zero"] = EXPECTED_DROP_TO_ZERO
        report["p11_drop_total"] = p11_drop_total

    base = "collect_window_%s%s%s_%s" % ("DRYRUN_" if dry else "", "P11_" if p11 else "", WIN_START, WIN_END_EXCL)
    out_path = os.path.join(data_dir, base + ".json")
    parquet_path = os.path.join(data_dir, base + "_pairs.parquet")
    out = {"report": report, "pairs": valid_json}
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out_path)
    write_pairs_parquet(parquet_path, pair_rows, p11=p11)
    bad_csv_path = _write_bad_prices_csv(data_dir, bad_price_records)

    print("\n[out] deystvitelnyh par: %d -> %s" % (len(valid_json), out_path))
    print("[out] porchennyh cen vne [0,1] (POPRAVKA 10): %d -> %s" % (len(bad_price_records), bad_csv_path))
    print("[out] vseh par (vkl. vybyvshie) v parquet: %d -> %s" % (len(pair_rows), parquet_path))
    print("[note] survivors (skrin CLV>0 & t>3) zdes' NE schitayutsya -- otdelnaya stadiya.")
    if dry:
        print("[note] DRYRUN: rezultat v reshenie sec.7 NE vhodit, vybrasyvaetsya.")
    return out


def mode_enum(data_dir):
    markets_all = enumerate_window(ENUM_END_MIN, ENUM_END_MAX)
    in_window, comp, gsts = window_composition(markets_all)
    print("\n===== STROKA SOSTAVA TIROV V OKNE =====")
    for t in TIERS:
        c = comp[t]
        print("  %-3s: v okne(gst)=%-5d slug_v_okne=%-5d gst_net=%-4d gst_vne=%-4d"
              % (t.upper(), c["gst_in_win"], c["slug_in_win"], c["gst_missing"], c["gst_out"]))
    atp_wta = comp["atp"]["gst_in_win"] + comp["wta"]["gst_in_win"]
    print("[control] ATP+WTA v okne (gst) = %d (dopustimo %d..%d)" % (atp_wta, CONTROL_LO, CONTROL_HI))


# ------------------------------- selftest --------------------------------
# ============================ POPRAVKA 12 (Dif 2) ============================
# Ves' kod nizhe -- NOVYY. Prohod A po seti (source='network') pobitovo NE zatragivaetsya.
def _repull_paths(data_dir):
    d = os.path.join(data_dir, REPULL_DIR_NAME)
    return d, os.path.join(d, REPULL_MANIFEST), os.path.join(d, REPULL_LOCK)


def _sha256_file(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _repull_raw_get(url):
    """Odna popytka (throttle vnutri). Vozvrat: dannye libo {'__error__':..,'__transient__':bool}."""
    throttle()
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"__error__": "HTTP %d %s" % (e.code, e.reason), "__transient__": (e.code in TRANSIENT)}
    except urllib.error.URLError as e:
        return {"__error__": "conn: %s" % (e.reason,), "__transient__": False}
    except Exception as e:
        return {"__error__": "%s: %s" % (type(e).__name__, e), "__transient__": False}


def repull_get_or_die(url, what):
    """POPRAVKA12: retry TOLKO na transient, backoff 1->2->4->8->16 s. Inache -- padenie srazu."""
    last = None
    for i, back in enumerate((0.0,) + REPULL_BACKOFF):   # popytka 0 bez pauzy; dalee pauzy 1,2,4,8,16
        if back:
            time.sleep(back)
        r = _repull_raw_get(url)
        if not (isinstance(r, dict) and r.get("__error__")):
            if i:
                with _ret_lock:
                    _retries_ok[0] += i         # chislo uspeshnyh povtorov (kak v get())
            return r
        last = r
        if not r.get("__transient__"):
            raise AmbiguousInput("re-pull zapros ne udalsya, padenie srazu (%s): %s -> %s"
                                 % (what, url, r["__error__"]))
    raise AmbiguousInput("re-pull: ischerpany %d povtorov (%s): %s -> %s"
                         % (len(REPULL_BACKOFF), what, url, last["__error__"]))


def pull_trades_manifest(cond):
    """POPRAVKA12: vse dostizhimye sdelki rynka dlya re-pull; paginaciya proven (limit, offset {0,OFFMAX}).
    Vozvrat: (rows, n_pages, offset_cap_hit, completeness_unreachable).
      offset_cap_hit           -- prishlos' zaprashivat' stranicu na offset=TRADES_OFFMAX (potolok).
      completeness_unreachable -- OBE stranicy polnye: hvost samyh staryh (predmatch) sdelok nedostizhim."""
    rows = []; n_pages = 0; offset_cap_hit = False
    for off in (0, TRADES_OFFMAX):
        qs = urllib.parse.urlencode({"market": cond, "limit": TRADES_LIMIT, "offset": off})
        r = repull_get_or_die(DATA + "/trades?" + qs, "re-pull trades cond=%s off=%d" % (cond, off))
        if not isinstance(r, list):
            raise AmbiguousInput("re-pull trades: otvet ne spisok cond=%s off=%d" % (cond, off))
        n_pages += 1
        rows.extend(r)
        if off == TRADES_OFFMAX:
            offset_cap_hit = True
        if len(r) < TRADES_LIMIT:
            return rows, n_pages, offset_cap_hit, False     # doshli do nachala istorii -> polno
    return rows, n_pages, True, True                        # obe polnye -> nepolno (nedostizhim hvost)


def write_trades_raw_win(data_dir, cond, rows):
    """Atomarnaya zapis' syryh sdelok rynka (list dict, kak ot API) v trades_raw_win/. Vozvrat (fname, sha)."""
    d, _, _ = _repull_paths(data_dir)
    if not os.path.isdir(d):
        os.makedirs(d)
    fname = "trades_%s.json" % cond
    fp = os.path.join(d, fname); tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp, fp)
    return fname, _sha256_file(fp)


def load_repull_manifest(data_dir):
    """cond -> zapis' manifesta; TOLKO status ok, fayl na meste i sha sovpadaet (validaciya resume)."""
    d, mpath, _ = _repull_paths(data_dir)
    raw = {}
    if not os.path.exists(mpath):
        return {}
    with open(mpath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            raw[rec["cond"]] = rec          # poslednyaya zapis' pobezhdaet
    ok = {}
    for cond, rec in raw.items():
        if rec.get("status") != "ok":
            continue
        fp = os.path.join(d, rec.get("file", ""))
        if rec.get("file") and os.path.exists(fp) and _sha256_file(fp) == rec.get("sha"):
            ok[cond] = rec
    return ok


def append_repull_manifest(data_dir, rec):
    d, mpath, _ = _repull_paths(data_dir)
    if not os.path.isdir(d):
        os.makedirs(d)
    with open(mpath, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush(); os.fsync(f.fileno())


def read_trades_raw_win(data_dir, cond):
    """Dlya source='raw_win': (rows, complete). complete = NE completeness_unreachable (po manifestu)."""
    d, _, _ = _repull_paths(data_dir)
    rec = load_repull_manifest(data_dir).get(cond)
    if rec is None:
        raise AmbiguousInput("re-pull raw_win: net gotovyh sdelok dlya cond=%s (snachala 'repull')" % cond)
    with open(os.path.join(d, rec["file"]), "r", encoding="utf-8") as f:
        rows = json.load(f)
    return rows, (not rec.get("completeness_unreachable", False))


def acquire_repull_lock(data_dir):
    """Guard (POPRAVKA12 p.6): ne zapuskat' re-pull parallelno s kollektorom (DuckDB odnopotochna na zapis')."""
    d, _, lpath = _repull_paths(data_dir)
    if not os.path.isdir(d):
        os.makedirs(d)
    try:
        fd = os.open(lpath, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise AmbiguousInput(
            "re-pull LOCK zanyat (%s): vozmozhno idyot kollektor (clob/book) ili proshlyy re-pull. "
            "NE zapuskat' parallelno. Esli tochno nichego ne rabotaet -- udali lock vruchnuyu." % lpath)
    os.write(fd, ("pid=%d ts=%s\n" % (os.getpid(), iso_ts(int(time.time())))).encode())
    os.close(fd)
    return lpath


def release_repull_lock(data_dir):
    _, _, lpath = _repull_paths(data_dir)
    try:
        os.remove(lpath)
    except FileNotFoundError:
        pass


def _repull_missing(target_conds, manifest_conds):
    mset = set(manifest_conds)
    return [c for c in target_conds if c not in mset]


def repull(data_dir, enum_min, enum_max):
    """POPRAVKA12 Dif2 (RUN tolko po slovu): sbor syryh /trades po vsem vnutriokonnym ATP/WTA rynkam v
    trades_raw_win/. Manifest na rynok, resume, retry 5x(1..16s) tolko transient, coverage fail-fast,
    itogi kazhdye 200. Guard-lock ot parallelnogo zapuska s kollektorom."""
    set_window(WIN_START, WIN_END_EXCL)
    d, mpath, _ = _repull_paths(data_dir)
    acquire_repull_lock(data_dir)
    try:
        markets_all = enumerate_window(enum_min, enum_max)
        in_window, comp, gsts = window_composition(markets_all)
        targets = [m for m in in_window.values() if m["tier"] in DECISION_TIERS]
        if not targets:
            raise AmbiguousInput("re-pull: net ATP/WTA rynkov v okne -- proveryat' okno/enumeraciyu")
        atp_wta = comp["atp"]["gst_in_win"] + comp["wta"]["gst_in_win"]
        print("[repull] rynkov-celey ATP/WTA v okne = %d (kontrol %d..%d, throttle %d/10s, workers=%d)"
              % (len(targets), CONTROL_LO, CONTROL_HI, MAX_PER_10S, WORKERS))
        if not (CONTROL_LO <= atp_wta <= CONTROL_HI):
            raise AmbiguousInput("re-pull KONTROL PROVALEN: ATP+WTA=%d vne [%d..%d] -> STOP DO sbora"
                                 % (atp_wta, CONTROL_LO, CONTROL_HI))
        done = load_repull_manifest(data_dir)
        todo = [m for m in targets if m["cond"] not in done]
        print("[repull] gotovo (resume) = %d; k sboru = %d" % (len(done), len(todo)))
        t0 = time.time(); n_done = 0; n_unreach = 0
        for m in todo:
            rows, n_pages, offset_cap_hit, unreach = pull_trades_manifest(m["cond"])
            fname, sha = write_trades_raw_win(data_dir, m["cond"], rows)
            append_repull_manifest(data_dir, {
                "cond": m["cond"], "slug": m["slug"], "tier": m["tier"],
                "gst": (int(m["gst"]) if m["gst"] is not None else None),
                "n_trades": len(rows), "n_pages": n_pages,
                "offset_cap_hit": bool(offset_cap_hit),
                "completeness_unreachable": bool(unreach),
                "file": fname, "sha": sha, "status": "ok"})
            n_done += 1; n_unreach += (1 if unreach else 0)
            if n_done % 200 == 0:
                el = time.time() - t0; rate = (n_done / el) if el else 0.0
                print("    [repull] %d/%d | %.1f rynkov/s | nedostizhimyh=%d | retry_ok=%d"
                      % (n_done, len(todo), rate, n_unreach, _retries_ok[0]))
        final = load_repull_manifest(data_dir)
        missing = _repull_missing([m["cond"] for m in targets], final.keys())
        cov = len(targets) - len(missing)
        print("[repull] coverage: %d/%d rynkov v manifeste (ok)" % (cov, len(targets)))
        if missing:
            raise AmbiguousInput("re-pull COVERAGE PROVALEN: ne sobrano %d/%d -> STOP. Primer: %s"
                                 % (len(missing), len(targets), ", ".join(missing[:20])))
        n_unr_total = sum(1 for r in final.values() if r.get("completeness_unreachable"))
        print("[repull] GOTOVO: %d rynkov | nedostizhimyh predmatch=%d | retry_ok=%d -> %s"
              % (len(targets), n_unr_total, _retries_ok[0], d))
    finally:
        release_repull_lock(data_dir)


def _load_parquet_ntrades_sum(path):
    import pyarrow.parquet as pq
    col = pq.read_table(path, columns=["n_trades"]).column("n_trades").to_pylist()
    return sum(int(x) for x in col if x is not None)


def _passa_compare(frozen_pairs, new_pairs, sum_frozen, sum_new, tol=PASSA_TOL):
    """Chistaya sverka Prohoda A s frozen: nabor par sovpadaet, chislovye polya v predelah tol,
    sum(n_trades) bit-v-bit. Rasxozhdenie -> AmbiguousInput (STOP). Vozvrat: dict s max |d|."""
    def key(p):
        return (p["wallet"], p["cond"])
    fmap = {key(p): p for p in frozen_pairs}
    nmap = {key(p): p for p in new_pairs}
    if set(fmap) != set(nmap):
        of = list(set(fmap) - set(nmap))[:10]; on = list(set(nmap) - set(fmap))[:10]
        raise AmbiguousInput("verify-passA STOP: nabor par razlichaetsya (tolko_frozen=%d, tolko_new=%d) "
                             "primer_f=%s primer_n=%s"
                             % (len(set(fmap) - set(nmap)), len(set(nmap) - set(fmap)), of, on))
    NUM = ("N", "entry_vwap", "p_ref", "clv")
    worst = 0.0; worst_at = None
    for k in fmap:
        a = fmap[k]; b = nmap[k]
        for fld in NUM:
            fa = a.get(fld); fb = b.get(fld)
            if fa is None or fb is None:
                if (fa is None) != (fb is None):
                    raise AmbiguousInput("verify-passA STOP: %s.%s None-rasxozhdenie (f=%r n=%r)"
                                         % (k, fld, fa, fb))
                continue
            dd = abs(float(fa) - float(fb))
            if dd > worst:
                worst = dd; worst_at = (k, fld)
            if dd > tol:
                raise AmbiguousInput("verify-passA STOP: %s.%s |d|=%.3e > %.0e" % (k, fld, dd, tol))
    if int(sum_frozen) != int(sum_new):
        raise AmbiguousInput("verify-passA STOP: sum(n_trades) ne bit-v-bit: frozen=%d new=%d (raznica=%d)"
                             % (int(sum_frozen), int(sum_new), int(sum_new) - int(sum_frozen)))
    return {"pairs": len(fmap), "max_abs_delta": worst, "max_at": worst_at, "sum_n_trades": int(sum_frozen)}


def verify_pass_a(data_dir, frozen_json=None, frozen_parquet=None):
    """POPRAVKA12 (DO pereschyota): dokazat', chto Prohod A iz trades_raw_win/ vosproizvodit frozen
    (chislovye polya <=1e-9, sum(n_trades) bit-v-bit). Rasxozhdenie -> STOP, pereschyot NE zapuskaetsya.
    Vyhod Prohoda A pishetsya vo VREMENNUYU papku -- frozen fayly NE perezapisyvayutsya."""
    frozen_json = frozen_json or os.path.join(data_dir, FROZEN_JSON_DEFAULT)
    frozen_parquet = frozen_parquet or os.path.join(data_dir, FROZEN_PARQUET_DEFAULT)
    if not (os.path.exists(frozen_json) and os.path.exists(frozen_parquet)):
        raise AmbiguousInput("verify-passA: net frozen fajlov (%s / %s)" % (frozen_json, frozen_parquet))
    tmp_out = os.path.join(data_dir, "_verify_passA_tmp")
    if not os.path.isdir(tmp_out):
        os.makedirs(tmp_out)
    print("[verify-passA] pereschyot Prohoda A iz trades_raw_win/ (bez seti); frozen NE trogaem")
    out = collect(tmp_out, ENUM_END_MIN, ENUM_END_MAX, do_control=True, dry=False, p11=False,
                  source="raw_win", source_dir=data_dir)
    new_pairs = out["pairs"]
    with open(frozen_json, "r", encoding="utf-8") as f:
        frozen_pairs = json.load(f)["pairs"]
    new_parquet = os.path.join(tmp_out, FROZEN_PARQUET_DEFAULT)
    res = _passa_compare(frozen_pairs, new_pairs,
                         _load_parquet_ntrades_sum(frozen_parquet),
                         _load_parquet_ntrades_sum(new_parquet))
    print("[verify-passA] OK: par=%d | max|d|=%.3e (<=%.0e) | sum(n_trades)=%d bit-v-bit"
          % (res["pairs"], res["max_abs_delta"], PASSA_TOL, res["sum_n_trades"]))
    return res


def _cascade_count(frozen_valid, passb_valid, min_matches=MIN_MATCHES):
    """Chistyy schet (AM1, voronku NE perestraivaem): pary poteryany pod P11, (wallet,tier) s perehodom
    >=min_matches -> <min_matches, rynki poteryavshie >=1 paru."""
    def key(p):
        return (p["wallet"], p["cond"])
    bkeys = set(key(p) for p in passb_valid)
    lost = [p for p in frozen_valid if key(p) not in bkeys]
    before = collections.defaultdict(int); after = collections.defaultdict(int)
    for p in frozen_valid:
        before[(p["wallet"], p["tier"])] += 1
    for p in passb_valid:
        after[(p["wallet"], p["tier"])] += 1
    crossed = [wt for wt in before if before[wt] >= min_matches and after.get(wt, 0) < min_matches]
    markets = set(p["cond"] for p in lost)
    return {"pairs_lost": len(lost), "wallets_crossed_down": len(crossed),
            "markets_losing_pairs": len(markets)}


def cascade_probe(data_dir, frozen_json=None):
    """POPRAVKA12 (AM1) count-only: skolko koshelkov perehodyat >=100 -> <100 i skolko rynkov teryayut
    pary pod P11. Voronka NE perestraivaetsya. Vyhod Prohoda B -- vo vremennuyu papku."""
    frozen_json = frozen_json or os.path.join(data_dir, FROZEN_JSON_DEFAULT)
    if not os.path.exists(frozen_json):
        raise AmbiguousInput("cascade-probe: net frozen %s" % frozen_json)
    with open(frozen_json, "r", encoding="utf-8") as f:
        frozen_valid = json.load(f)["pairs"]
    tmp_out = os.path.join(data_dir, "_cascade_tmp")
    if not os.path.isdir(tmp_out):
        os.makedirs(tmp_out)
    print("[cascade] Prohod B iz trades_raw_win/ (bez seti); count-only, voronka NE perestraivaetsya")
    outB = collect(tmp_out, ENUM_END_MIN, ENUM_END_MAX, do_control=True, dry=False, p11=True,
                   source="raw_win", source_dir=data_dir)
    res = _cascade_count(frozen_valid, outB["pairs"])
    print("[cascade] par poteryano pod P11: %d" % res["pairs_lost"])
    print("[cascade] (wallet,tier) perehod >=%d -> <%d: %d"
          % (MIN_MATCHES, MIN_MATCHES, res["wallets_crossed_down"]))
    print("[cascade] rynkov, poteryavshih >=1 paru: %d" % res["markets_losing_pairs"])
    return res


def _selftest():
    set_window("2026-02-01", "2026-04-28")
    assert tier_of("atp-sinner-alcaraz-2026-03-15") == "atp"
    assert tier_of("wta-swiatek-gauff-2026-02-02") == "wta"
    assert tier_of("itf-x-y-2026-02-10") == "itf"
    assert tier_of("atp-doubles-a-b-2026-03-15") is None
    assert tier_of("nba-lakers-2026-03-15") is None
    assert slug_date("atp-a-b-2026-03-15") == "2026-03-15" and slug_date("atp-final") is None
    assert is_singles("atp-a-b-2026-03-15") and not is_singles("atp-doubles-a-2026-03-15")
    assert slug_date_in_window("atp-a-b-2026-02-01") and slug_date_in_window("atp-a-b-2026-04-27")
    assert not slug_date_in_window("atp-a-b-2026-04-28") and not slug_date_in_window("atp-a-b-2026-01-31")
    g_in = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp())
    g_28 = int(datetime(2026, 4, 28, tzinfo=timezone.utc).timestamp())
    assert gst_in_window(g_in) and not gst_in_window(g_28) and not gst_in_window(None)

    # dryrun window switch
    set_window("2026-02-01", "2026-02-08")
    assert slug_date_in_window("atp-a-b-2026-02-07") and not slug_date_in_window("atp-a-b-2026-02-08")
    set_window("2026-02-01", "2026-04-28")

    for bad in ({"clobTokenIds": None}, {"clobTokenIds": ["a"]}, {"clobTokenIds": ["a", "a"]},
                {"clobTokenIds": ["a", "b", "c"]}):
        try:
            parse_clob(bad); assert False, bad
        except AmbiguousInput:
            pass
    assert parse_clob({"clobTokenIds": "[\"tk0\",\"tk1\"]"}) == ["tk0", "tk1"]

    clob = ["tk0", "tk1"]; cs = set(clob)
    assert idx_of({"asset": "tk0"}, clob, cs) == 0 and idx_of({"asset": "tk1"}, clob, cs) == 1
    assert idx_of({"outcomeIndex": 1}, clob, cs) == 1
    try:
        idx_of({"asset": "zzz", "outcomeIndex": 5}, clob, cs); assert False
    except AmbiguousInput:
        pass

    # svyortka sec.3
    tr = [{"asset": "tk0", "side": "BUY", "price": 0.40, "size": 10},
          {"asset": "tk1", "side": "BUY", "price": 0.30, "size": 4}]
    N, entry, direction, two, _u1 = convolve(tr, clob, cs, "c1")
    assert abs(N - 6.0) < 1e-9 and direction == 1 and abs(entry - 0.40) < 1e-9 and two is True
    tr0 = [{"asset": "tk0", "side": "BUY", "price": 0.5, "size": 5},
           {"asset": "tk0", "side": "SELL", "price": 0.6, "size": 5}]
    N0, e0, d0, tw0, _u0 = convolve(tr0, clob, cs, "c0")
    assert abs(N0) < 1e-9 and e0 is None and d0 == 0 and tw0 is True
    trs = [{"asset": "tk1", "side": "BUY", "price": 0.20, "size": 8}]
    Ns, es, ds, tws, _us = convolve(trs, clob, cs, "cs")
    assert abs(Ns + 8.0) < 1e-9 and ds == -1 and abs(es - 0.80) < 1e-9 and tws is False
    trv = [{"asset": "tk0", "side": "BUY", "price": 0.40, "size": 10},
           {"asset": "tk0", "side": "BUY", "price": 0.50, "size": 30},
           {"asset": "tk0", "side": "SELL", "price": 0.90, "size": 5}]
    Nv, ev, dv, twv, _uv = convolve(trv, clob, cs, "cv")
    assert abs(Nv - 35.0) < 1e-9 and dv == 1 and abs(ev - 0.475) < 1e-9 and twv is True
    for bad in ([{"asset": "tk0", "side": "BUY", "price": 1.4, "size": 1}],
                [{"asset": "tk0", "side": "HOLD", "price": 0.5, "size": 1}],
                [{"asset": "tk0", "side": "BUY", "price": 0.5, "size": 0}]):
        try:
            convolve(bad, clob, cs, "cx"); assert False
        except AmbiguousInput:
            pass

    # clv sec.4
    assert abs(clv_of(1, 0.40, 0.65) - 0.25) < 1e-9   # long: p_ref-entry
    assert abs(clv_of(-1, 0.80, 0.65) - 0.15) < 1e-9  # short: entry-p_ref

    # etalon sec.2 + DOBAVKA3 (isklyuchenie term-cen)
    rows = [{"timestamp": 940, "asset": "tk0", "price": 0.7},
            {"timestamp": 980, "asset": "tk1", "price": 0.35},
            {"timestamp": 1005, "asset": "tk0", "price": 0.9}]
    line0, age = market_etalon(rows, 1000, clob, cs, "c")
    assert abs(line0 - 0.65) < 1e-9 and age == 20
    # term-cena kak poslednyaya do gst -> propuskaetsya, beryotsya predydushchaya rynochnaya
    rows_t = [{"timestamp": 940, "asset": "tk0", "price": 0.62},
              {"timestamp": 985, "asset": "tk0", "price": 0.9995}]  # raschyotnaya -> ignor
    l_t, a_t = market_etalon(rows_t, 1000, clob, cs, "c")
    assert abs(l_t - 0.62) < 1e-9 and a_t == 60
    assert market_etalon([{"timestamp": 1005, "asset": "tk0", "price": 0.9}], 1000, clob, cs, "c") == (None, None)
    l3, a3 = market_etalon([{"timestamp": 1000 - 4000, "asset": "tk0", "price": 0.7}], 1000, clob, cs, "c")
    assert l3 == 0.7 and a3 == 4000 and a3 > ETALON_MAX_AGE
    assert is_term_price(0.9995) and is_term_price(0.0005) and not is_term_price(0.5)

    # terminal winner
    assert terminal_winner([{"timestamp": 5, "asset": "tk0", "price": 0.97},
                            {"timestamp": 6, "asset": "tk1", "price": 0.02}], clob, cs, "c") == 0
    assert terminal_winner([{"timestamp": 5, "asset": "tk0", "price": 0.05},
                            {"timestamp": 6, "asset": "tk1", "price": 0.98}], clob, cs, "c") == 1
    assert terminal_winner([{"timestamp": 5, "asset": "tk0", "price": 0.55},
                            {"timestamp": 6, "asset": "tk1", "price": 0.45}], clob, cs, "c") is None

    # POPRAVKA 10: filtr syryh cen vne [0,1] (DO konversii komplementa)
    bp_rows = [
        {"timestamp": 900, "asset": "tk0", "price": 0.40, "size": 3, "side": "BUY",
         "proxyWallet": "0xW1", "outcomeIndex": 0, "transactionHash": "0xh1"},
        {"timestamp": 950, "asset": "tk0", "price": 1.68, "size": 5, "side": "SELL",
         "proxyWallet": "0xW1", "outcomeIndex": 0, "transactionHash": "0xh2"},
        {"timestamp": 970, "asset": "tk1", "price": -0.68, "size": 2, "side": "BUY",
         "proxyWallet": "0xW2", "outcomeIndex": 1, "transactionHash": "0xh3"},
    ]
    cl, bd = filter_bad_prices(bp_rows, 1000, "cbp", "wta-a-b-2026-02-01")
    assert len(cl) == 1 and len(bd) == 2
    assert bd[0]["price"] == 1.68 and bd[0]["pre_match"] == 1 and bd[0]["proxyWallet"] == "0xW1"
    assert bd[1]["price"] == -0.68 and bd[1]["conditionId"] == "cbp" and bd[1]["outcomeIndex"] == 1
    assert all(0.0 <= float(t["price"]) <= 1.0 for t in cl)
    cl2, bd2 = filter_bad_prices([{"price": 0.0, "timestamp": 1}, {"price": 1.0, "timestamp": 2}],
                                 1000, "c", "s")
    assert len(cl2) == 2 and bd2 == []
    assert terminal_winner([{"timestamp": 5, "asset": "tk0", "price": 0.9}], clob, cs, "c") is None

    # DOPUSK TIRA
    day = 86400
    gl = [1_000_000 + (i * 70 * day) // 149 for i in range(150)]
    span, ok = tier_admission(150, gl)
    assert ok is True and span >= 60
    span2, ok2 = tier_admission(99, gl)
    assert ok2 is False                       # <100 matchey
    gl_narrow = [1_000_000 + (i * 10 * day) // 149 for i in range(150)]
    span3, ok3 = tier_admission(150, gl_narrow)
    assert ok3 is False                       # razmah <60 dney

    # quantile
    q = sorted([1, 2, 3, 4, 100])
    assert quantile(q, 0.5) == 3 and quantile([], 0.5) is None and quantile([7], 0.9) == 7.0

    # pull_trades completeness logika (bez seti): imitiruem cherez podmenu get_or_die
    global get_or_die
    orig = get_or_die
    try:
        pages = {0: [{"x": i} for i in range(TRADES_LIMIT)], TRADES_OFFMAX: [{"y": 1}]}
        get_or_die = lambda url, what: pages[int(dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))["offset"])]
        rws, comp_ok = pull_trades("cA")
        assert comp_ok is True and len(rws) == TRADES_LIMIT + 1     # 2-ya stranica korotkaya -> polno
        pages2 = {0: [{"x": i} for i in range(TRADES_LIMIT)],
                  TRADES_OFFMAX: [{"y": i} for i in range(TRADES_LIMIT)]}
        get_or_die = lambda url, what: pages2[int(dict(urllib.parse.parse_qsl(url.split("?", 1)[1]))["offset"])]
        rws2, comp_ok2 = pull_trades("cB")
        assert comp_ok2 is False and len(rws2) == 2 * TRADES_LIMIT   # obe polnye -> nepolno
        get_or_die = lambda url, what: [{"z": 1}]                    # pervaya korotkaya -> polno
        rws3, comp_ok3 = pull_trades("cC")
        assert comp_ok3 is True and len(rws3) == 1
    finally:
        get_or_die = orig

    # parquet round-trip (esli pyarrow dostupen)
    try:
        import pyarrow  # noqa: F401
        rows_pq = [
            {"wallet": "0xa", "slug": "atp-x-2026-02-03", "tier": "atp", "gameStartTime": 111,
             "n_trades": 3, "entry_vwap": 0.4, "p_ref": 0.65, "p_ref_age_min": 1.0, "clv": 0.25,
             "dropped_reason": ""},
            {"wallet": "0xb", "slug": "wta-y-2026-02-04", "tier": "wta", "gameStartTime": 222,
             "n_trades": 0, "entry_vwap": None, "p_ref": None, "p_ref_age_min": None, "clv": None,
             "dropped_reason": "no_prematch"}]
        tmpdir = tempfile.mkdtemp()
        pth = os.path.join(tmpdir, "t.parquet")
        write_pairs_parquet(pth, rows_pq)
        import pyarrow.parquet as pq
        back = pq.read_table(pth).to_pylist()
        assert len(back) == 2 and back[0]["clv"] == 0.25 and back[1]["entry_vwap"] is None
        assert "n_trades_used_p11" not in back[0]          # Prohod A: kolonki net (frozen-sovmestimo)
        rows_p11 = [dict(rows_pq[0], n_trades_used_p11=2), dict(rows_pq[1], n_trades_used_p11=0)]
        pth2 = os.path.join(tmpdir, "t_p11.parquet")
        write_pairs_parquet(pth2, rows_p11, p11=True)
        back2 = pq.read_table(pth2).to_pylist()
        assert back2[0]["n_trades_used_p11"] == 2 and list(back2[0].keys()).index("n_trades_used_p11") == 5
        parquet_state = "OK (+p11 col posle n_trades)"
    except ImportError:
        parquet_state = "SKIP (pyarrow net v sandbox; na venv Johna pyarrow est')"

    # ================= POPRAVKA 11: storona vhoda (oracle = p11_entry.py, frozen 2eef68c5) =================
    import importlib.util as _ilu
    _p11_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "p11_entry.py")
    _spec = _ilu.spec_from_file_location("p11_entry", _p11_path)
    _oracle = _ilu.module_from_spec(_spec); _spec.loader.exec_module(_oracle)
    assert _oracle.DROP_REASON_P11 == DROP_REASON_P11
    assert _oracle.EXPECTED_DROP_TO_ZERO == EXPECTED_DROP_TO_ZERO
    assert _oracle.TERM_HI == TERM_HI and _oracle.TERM_LO == TERM_LO

    def _cvt(oi, side, price, size):   # convolve chitaet asset/outcomeIndex
        return {"asset": ("tk0" if oi == 0 else "tk1"), "outcomeIndex": oi,
                "side": side, "price": price, "size": size}
    def _ort(oi, side, price, size):   # oracle chitaet oi/side/price/size
        return {"oi": oi, "side": side, "price": price, "size": size}
    def _apN(a, b, eps=1e-9):
        return (a is None and b is None) or (a is not None and b is not None and abs(a - b) <= eps)

    def _xcheck(trades, p_ref):
        cv = [_cvt(*x) for x in trades]; orc = [_ort(*x) for x in trades]
        r = _oracle.recompute_pair(orc, p_ref)
        # Prohod A (p11=False): entry POBITOVO == zamorozhennyy == oracle entry_a
        Na, ea, da, _, ua = convolve(cv, clob, cs, "cx", p11=False)
        assert _apN(ea, r["entry_a"]), ("A", ea, r["entry_a"], trades)
        assert (da == 0) == r["net_zero"], (da, r, trades)
        # Prohod B (p11=True): entry_b/used/drop == oracle
        Nb, eb, db, _, ub = convolve(cv, clob, cs, "cx", p11=True)
        assert _apN(eb, r["entry_b"]), ("B", eb, r["entry_b"], trades)
        assert ub == r["n_trades_used_p11"], ("used", ub, r, trades)
        drop_b = (abs(Nb) >= 1e-9 and eb is None)
        assert drop_b == r["dropped_p11"], ("drop", drop_b, r, trades)
        return r

    r1 = _xcheck([(0, "BUY", 0.40, 10)], 0.65)            # obychnaya: A==B, used=1
    assert r1["n_trades_used_p11"] == 1 and not r1["dropped_p11"]
    r2 = _xcheck([(1, "BUY", 0.999, 5)], 0.5)            # oi=1 SYRAYA 0.999 -> DROP (dyra 0.999)
    assert r2["dropped_p11"] and r2["n_trades_used_p11"] == 0 and r2["direction"] != 0
    r3 = _xcheck([(1, "BUY", 0.001, 5)], 0.5)            # oi=1 SYRAYA 0.001 -> DROP
    assert r3["dropped_p11"] and r3["n_trades_used_p11"] == 0
    r4 = _xcheck([(0, "BUY", 0.9995, 10), (0, "BUY", 0.40, 30)], 0.65)   # term iskl., used=1, ne drop
    assert r4["n_trades_used_p11"] == 1 and not r4["dropped_p11"] and abs(r4["entry_b"] - 0.40) < 1e-9
    r5 = _xcheck([(0, "BUY", 0.5, 5), (0, "SELL", 0.6, 5)], 0.5)         # net-zero -> ne P11
    assert r5["net_zero"] and not r5["dropped_p11"]
    r6 = _xcheck([(0, "BUY", 0.40, 10), (0, "BUY", 0.50, 30)], 0.65)     # A ne menyaet frozen entry
    assert abs(r6["entry_a"] - 0.475) < 1e-9

    # drop-gate na sinteticheskom nabore: rovno K par s pustym vhodom
    K = 7
    synth = [[(1, "BUY", 0.999, 5)] for _ in range(K)] + [[(0, "BUY", 0.40, 10)] for _ in range(3)]
    drop_cnt = 0
    for tr_ in synth:
        cvv = [_cvt(*x) for x in tr_]
        Nb, eb, db, _, ub = convolve(cvv, clob, cs, "cg", p11=True)
        if abs(Nb) >= 1e-9 and eb is None:
            drop_cnt += 1
    assert drop_cnt == K, drop_cnt
    assert _oracle.check_drop_gate(drop_cnt, expected=K) is True
    try:
        _oracle.check_drop_gate(drop_cnt + 1, expected=K); assert False
    except SystemExit:
        pass
    assert _oracle.assert_ntrades_sum_invariant(123, 123) is True
    try:
        _oracle.assert_ntrades_sum_invariant(123, 124); assert False
    except SystemExit:
        pass
    p11_state = "OK (oracle 2eef68c5: A==frozen, B==oracle, used, dyra-0.999, oi=1x2, gate, sum-inv)"

    # ================= POPRAVKA 12 (Dif 2): re-pull + harness + kaskad (offline, cherez moki) =================
    import tempfile as _tf, urllib.parse as _up
    global _repull_raw_get, REPULL_BACKOFF, repull_get_or_die

    # -- repull_get_or_die: retry tolko transient; backoff podmenyaem na 0 (bez realnyh pauz); uchyot retry_ok --
    _save_raw = _repull_raw_get; _save_bo = REPULL_BACKOFF
    REPULL_BACKOFF = (0, 0, 0, 0, 0)
    try:
        seq = [{"__error__": "HTTP 503 x", "__transient__": True},
               {"__error__": "HTTP 502 y", "__transient__": True},
               [{"ok": 1}]]
        _repull_raw_get = lambda url, _s=seq: _s.pop(0)
        _r0 = _retries_ok[0]
        _out = repull_get_or_die("u", "w")
        assert _out == [{"ok": 1}] and _retries_ok[0] == _r0 + 2, (_out, _retries_ok[0] - _r0)
        _repull_raw_get = lambda url: {"__error__": "HTTP 400 bad", "__transient__": False}   # non-transient -> srazu
        try:
            repull_get_or_die("u", "w"); assert False
        except AmbiguousInput:
            pass
        _repull_raw_get = lambda url: {"__error__": "HTTP 503 z", "__transient__": True}       # ischerpanie -> raise
        try:
            repull_get_or_die("u", "w"); assert False
        except AmbiguousInput:
            pass
    finally:
        _repull_raw_get = _save_raw; REPULL_BACKOFF = _save_bo

    # -- pull_trades_manifest: n_pages / offset_cap_hit / completeness_unreachable --
    _save_god = repull_get_or_die
    try:
        def _mk(pmap):
            def _f(url, what):
                off = int(dict(_up.parse_qsl(url.split("?", 1)[1]))["offset"])
                return pmap[off]
            return _f
        repull_get_or_die = _mk({0: [{"x": i} for i in range(TRADES_LIMIT)], TRADES_OFFMAX: [{"y": 1}]})
        _rw, _npg, _cap, _unr = pull_trades_manifest("cA")
        assert _npg == 2 and _cap is True and _unr is False and len(_rw) == TRADES_LIMIT + 1
        repull_get_or_die = _mk({0: [{"z": 1}], TRADES_OFFMAX: []})
        _rw, _npg, _cap, _unr = pull_trades_manifest("cB")
        assert _npg == 1 and _cap is False and _unr is False and len(_rw) == 1
        repull_get_or_die = _mk({0: [{"x": i} for i in range(TRADES_LIMIT)],
                                 TRADES_OFFMAX: [{"y": i} for i in range(TRADES_LIMIT)]})
        _rw, _npg, _cap, _unr = pull_trades_manifest("cC")
        assert _npg == 2 and _cap is True and _unr is True and len(_rw) == 2 * TRADES_LIMIT
    finally:
        repull_get_or_die = _save_god

    # -- manifest + write/read + resume (validaciya sha) --
    _td = _tf.mkdtemp()
    _rows_a = [{"asset": "tk0", "price": 0.4, "size": 3, "side": "BUY", "timestamp": 900}]
    _fn, _sh = write_trades_raw_win(_td, "cM", _rows_a)
    append_repull_manifest(_td, {"cond": "cM", "slug": "atp-a-b-2026-02-03", "tier": "atp", "gst": 111,
                                 "n_trades": 1, "n_pages": 1, "offset_cap_hit": False,
                                 "completeness_unreachable": False, "file": _fn, "sha": _sh, "status": "ok"})
    _man = load_repull_manifest(_td)
    assert "cM" in _man and _man["cM"]["n_trades"] == 1
    _rr, _cr = read_trades_raw_win(_td, "cM")
    assert _cr is True and _rr == _rows_a
    append_repull_manifest(_td, {"cond": "cN", "file": "trades_cN.json", "sha": "deadbeef", "status": "ok"})
    assert "cN" not in load_repull_manifest(_td)          # net fayla/bityy sha -> resume soberyot zanovo
    _fn2, _sh2 = write_trades_raw_win(_td, "cU", _rows_a)
    append_repull_manifest(_td, {"cond": "cU", "file": _fn2, "sha": _sh2, "status": "ok",
                                 "completeness_unreachable": True})
    _, _cu = read_trades_raw_win(_td, "cU")
    assert _cu is False                                    # unreachable -> complete=False

    # -- lock: povtornyy zahvat padaet; posle osvobozhdeniya -- ok --
    _tl = _tf.mkdtemp()
    acquire_repull_lock(_tl)
    try:
        acquire_repull_lock(_tl); assert False
    except AmbiguousInput:
        pass
    release_repull_lock(_tl)
    acquire_repull_lock(_tl); release_repull_lock(_tl)

    # -- coverage helper --
    assert _repull_missing(["a", "b", "c"], ["a", "c"]) == ["b"]
    assert _repull_missing(["a", "b"], ["a", "b"]) == []

    # -- _passa_compare: sovpadenie v dopuske / prevyshenie / nabor par / sum(n_trades) --
    _fp = [{"wallet": "0xa", "cond": "c1", "tier": "atp", "N": 6.0, "entry_vwap": 0.4, "p_ref": 0.65, "clv": 0.25}]
    _np = [{"wallet": "0xa", "cond": "c1", "tier": "atp", "N": 6.0, "entry_vwap": 0.4 + 5e-10, "p_ref": 0.65, "clv": 0.25}]
    _ro = _passa_compare(_fp, _np, 6, 6)
    assert _ro["pairs"] == 1 and _ro["max_abs_delta"] <= PASSA_TOL and _ro["sum_n_trades"] == 6
    for _bn, _bsf, _bsn in [([dict(_np[0], entry_vwap=0.4 + 1e-6)], 6, 6),   # prevyshenie tol
                            ([dict(_np[0], cond="cZ")], 6, 6),               # nabor par
                            (_np, 6, 7)]:                                    # sum(n_trades) ne bit-v-bit
        try:
            _passa_compare(_fp, _bn, _bsf, _bsn); assert False
        except AmbiguousInput:
            pass

    # -- _cascade_count: poteri par / perehod koshelka 100->98 / rynki --
    _fv = ([{"wallet": "w1", "cond": "c%d" % i, "tier": "atp"} for i in range(100)] +
           [{"wallet": "w2", "cond": "d%d" % i, "tier": "wta"} for i in range(50)])
    _bv = ([{"wallet": "w1", "cond": "c%d" % i, "tier": "atp"} for i in range(98)] +
           [{"wallet": "w2", "cond": "d%d" % i, "tier": "wta"} for i in range(50)])
    _cc = _cascade_count(_fv, _bv)
    assert _cc["pairs_lost"] == 2 and _cc["wallets_crossed_down"] == 1 and _cc["markets_losing_pairs"] == 2

    d2_state = ("OK (repull retry/backoff-1..16 + manifest/resume-sha + offcap/unreach + lock + "
                "coverage + passA-1e-9+sumNt + cascade count-only)")

    # throttle concurrency
    errs = []
    def hammer():
        try:
            for _ in range(40):
                throttle()
        except Exception as e:
            errs.append(e)
    ts = [threading.Thread(target=hammer) for _ in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert not errs, errs
    with _lock:
        assert len(_win) <= MAX_PER_10S

    print("SELFTEST OK: tier/slug/window + dryrun-switch + parse_clob + idx + svyortka(sec.3) + "
          "clv(sec.4) + etalon(sec.2+term-excl) + winner + dopusk-tira(sec.5) + quantile + "
          "pull_trades-completeness + throttle + fail-fast | parquet=%s | P11=%s | D2=%s"
          % (parquet_state, p11_state, d2_state))


def _arg(name, default=None):
    pref = "--" + name + "="
    for a in sys.argv:
        if a.startswith(pref):
            return a[len(pref):]
        if a == "--" + name:
            return True
    return default

def main():
    if len(sys.argv) < 2:
        print(__doc__); return
    mode = sys.argv[1]
    data_dir = _arg("data-dir", "data")
    if isinstance(data_dir, bool):
        data_dir = "data"
    p11 = bool(_arg("p11"))   # POPRAVKA 11: Prohod B (pravilo ON). Bez flaga -- Prohod A (zamorozhennyy).
    source = _arg("source", "network")   # POPRAVKA12: 'network' (frozen) libo 'raw_win' (s diska re-pull)
    if isinstance(source, bool):
        source = "network"
    if mode == "selftest":
        _selftest()
    elif mode == "enum":
        set_window("2026-02-01", "2026-04-28")
        mode_enum(data_dir)
    elif mode == "run":
        set_window("2026-02-01", "2026-04-28")
        collect(data_dir, ENUM_END_MIN, ENUM_END_MAX, do_control=True, dry=False, p11=p11, source=source)
    elif mode == "dryrun":
        # DOBAVKA2: srez 2026-02-01..2026-02-07 (poluotkryto do 02-08). Kontrol 4068 NE primenyaetsya.
        set_window("2026-02-01", "2026-02-08")
        collect(data_dir, "2026-01-01", "2026-02-22", do_control=False, dry=True, p11=p11, source=source)
    elif mode == "repull":
        set_window("2026-02-01", "2026-04-28")
        repull(data_dir, ENUM_END_MIN, ENUM_END_MAX)
    elif mode == "verify-passa":
        set_window("2026-02-01", "2026-04-28")
        verify_pass_a(data_dir)
    elif mode == "cascade-probe":
        set_window("2026-02-01", "2026-04-28")
        cascade_probe(data_dir)
    else:
        print("neizvestnyy rezhim: %s" % mode); print(__doc__)

if __name__ == "__main__":
    main()
