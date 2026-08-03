#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PROBA 1 (GLM) -- est li tennisnye dannye za fevral-aprel 2026.

Zadacha: Zaplanirovana validaciya na netronutom okne 2026-02-01 .. 2026-05-01,
no CLOB_V2_LIVE_FROM = "2026-04-28". Okno pochti celikom do zapuska vtoroy
versii birzhi. Nado uznat, est li tam dannye voobshche.

Chto schitaem:
  1. Chislo tennisnyh matchevyh sobytiy (odinochki) s gameStartTime v okne,
     po mesyacam i po tigram ATP / WTA / ITF.
     Istochnik: /events?tag_slug=tennis, maska matchevogo slaga \\d{4}-\\d{2}-\\d{2}$,
     parnye (doubles) isklyuchayutsya.
  2. Otdaet li data-api/trades sdelki po etim rynkam. Syrye stroki otveta
     (po 3 matcha iz fevralya, marta, aprelya).
  3. Mediana chisla sdelok na match po (mesyac x tier).
  4. Dolya rynkov, gde est sdelka v predelah 60 minut do gameStartTime.
  5. Te zhe velichiny za tekuschee okno 2026-05-02 .. 2026-07-31 (sravnenie).
  6. Viden li razryv na granice 28 aprelya.

RAZBIYKA PO TIRAM: mediany i doli schitayutsya OTDELNO po ATP, WTA, ATP+WTA
i ITF. ITF opashivaetsya vyborochno (ITF_SAMPLE_SIZE matchey, fiksirovannoe
zerno ITF_SAMPLE_SEED), v statistiku ne vhodit -- tolko dlya opisaniya.
Obschaya mediana po vsem tigram pechataetsya s metkoy СМЕШАННАЯ_НЕ_ИСПОЛЬЗОВАТЬ.

KLYUCHEVOY NYUANS (ubytyy pri razvedke): event.endDate != match date.
gameStartTime (na urovne RYNKA) -- nastoyaschee vremya matcha. endDate otstaet
do ~2 nedel (rezolv). Poetomu: shirokiy srez po endDate, zatem filtr po
gameStartTime iz samogo rynka.

Potokobezopasnyy throttle (deque) ~140 req/10s, ThreadPoolExecutor dlya /trades.
Vse logi syrye v probes/glm/probe1_*.log. Skript nichego ne chinit.

usage:
  python probes/glm/probe1_tennis_window.py                          # oba okna
  python probes/glm/probe1_tennis_window.py --window current          # tolko tekuschee
  python probes/glm/probe1_tennis_window.py --window target           # tolko celevoe
  python probes/glm/probe1_tennis_window.py --window both             # oba (default)
  python probes/glm/probe1_tennis_window.py selftest                  # samotest
"""
import json, os, re, sys, time, random, statistics, threading, collections
import urllib.request, urllib.parse, urllib.error
from datetime import datetime, timezone, timedelta
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

GAMMA = os.environ.get("PM_GAMMA_HOST", "https://gamma-api.polymarket.com")
DATA = os.environ.get("PM_DATA_HOST", "https://data-api.polymarket.com")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
REQ_TIMEOUT = 45.0
OFFSET_CAP = 2000
RATE_WIN_S = 10.0
RATE_MAX = 140   # pod limit 150/10s s zapasom
WORKERS = 6
ITF_SAMPLE_SIZE = 300
ITF_SAMPLE_SEED = 20260803
_LOG_LOCK = threading.Lock()

SINGLES_RE = re.compile(r"\d{4}-\d{2}-\d{2}$")
DOUBLES_RE = re.compile(r"doubles", re.I)
TIER_RES = [
    ("ATP", re.compile(r"^atp-", re.I)),
    ("WTA", re.compile(r"^wta-", re.I)),
    ("ITF", re.compile(r"^itf-", re.I)),
]

LOGDIR = os.path.dirname(os.path.abspath(__file__))
RAWLOG = os.path.join(LOGDIR, "probe1_raw_api.log")
SUMMARY = os.path.join(LOGDIR, "probe1_summary.json")

WINDOW_TARGET = (datetime(2026, 2, 1, tzinfo=timezone.utc),
                 datetime(2026, 5, 1, tzinfo=timezone.utc))
WINDOW_CURRENT = (datetime(2026, 5, 2, tzinfo=timezone.utc),
                  datetime(2026, 7, 31, tzinfo=timezone.utc))
FETCH_PAD = timedelta(days=35)

# Potokobezopasnyy throttle
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


def log_raw(label, url, status, body):
    with _LOG_LOCK:
        with open(RAWLOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "label": label, "url": url,
                                "status": status, "body_len": len(body) if body else 0},
                               default=str) + "\n")


def get_json(url, retries=4):
    last_err = None
    for attempt in range(retries):
        throttle()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=REQ_TIMEOUT) as r:
                body = r.read().decode("utf-8", "replace")
                log_raw("GET", url, r.status, body)
                return json.loads(body), None
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


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(s):
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S+00", "%Y-%m-%dT%H:%M:%S.%fZ",
                "%Y-%m-%dT%H:%M:%S+00:00"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def tier_of(slug):
    s = (slug or "").lower()
    if DOUBLES_RE.search(s):
        return "DOUBLES"
    for name, rx in TIER_RES:
        if rx.search(s):
            return name
    return "OTHER"


def fetch_all_events(start, end, closed=None, slice_days=4):
    all_events = {}
    cur = start
    step = timedelta(days=slice_days)
    cap_hits = 0
    while cur < end:
        nxt = min(cur + step, end)
        lo, hi = iso(cur), iso(nxt)
        offset = 0
        while True:
            params = {"limit": 100, "offset": offset, "tag_slug": "tennis",
                      "end_date_min": lo, "end_date_max": hi,
                      "order": "endDate", "ascending": "true"}
            if closed is not None:
                params["closed"] = str(closed).lower()
            url = GAMMA + "/events?" + urllib.parse.urlencode(params)
            data, err = get_json(url)
            if err:
                print("    [events] ERR slice %s..%s offset %d: %s" % (lo, hi, offset, err))
                return list(all_events.values()), cap_hits, False
            if not isinstance(data, list):
                break
            if not data:
                break
            for ev in data:
                if isinstance(ev, dict) and ev.get("id") is not None:
                    all_events[ev["id"]] = ev
            if len(data) < 100:
                break
            offset += 100
            if offset >= OFFSET_CAP:
                cap_hits += 1
                print("    [events] CAP offset=%d slice %s..%s" % (offset, lo, hi))
                break
        cur = nxt
    return list(all_events.values()), cap_hits, True


def extract_singles_markets(events, window_start, window_end):
    out = []
    seen_cond = set()
    for ev in events:
        mkts = ev.get("markets")
        if not isinstance(mkts, list):
            continue
        for m in mkts:
            if not isinstance(m, dict):
                continue
            slug = m.get("slug") or ""
            cond = m.get("conditionId") or m.get("condition_id")
            if not cond or not slug:
                continue
            if DOUBLES_RE.search(slug):
                continue
            if not SINGLES_RE.search(slug):
                continue
            if cond in seen_cond:
                continue
            gst = parse_dt(m.get("gameStartTime"))
            tier = tier_of(slug)
            if tier == "DOUBLES":
                continue
            if gst is None:
                continue
            if not (window_start <= gst < window_end):
                continue
            seen_cond.add(cond)
            out.append({
                "conditionId": cond, "slug": slug, "tier": tier,
                "gameStartTime": gst.isoformat(), "gameStartTime_dt": gst,
                "event_slug": ev.get("slug", ""), "closedTime": m.get("closedTime"),
            })
    return out


def month_key(dt):
    return "%04d-%02d" % (dt.year, dt.month)


def count_by_month_tier(singles):
    by = defaultdict(lambda: defaultdict(int))
    for s in singles:
        mk = month_key(s["gameStartTime_dt"])
        by[mk][s["tier"]] += 1
        by[mk]["TOTAL"] += 1
    return by


def fetch_trades_stats(cond, gst_dt=None, window_min_s=3600):
    """Schitaet sdelki i proveryaet blizost k gameStartTime BEZ hraneniya vseh sdelok.
    Vozvraschaet (count, has_near_gamestart, first_rows_for_samples, err).
    Pamyatoeffektivno: ne kopit vse sdelki, tolko schetchik i flag.
    """
    count = 0
    has_near = False
    first_rows = []
    offset = 0
    limit = 500
    pages = 0
    gst_ts = gst_dt.timestamp() if gst_dt else None
    while pages < 60:
        params = {"market": cond, "limit": limit, "offset": offset}
        url = DATA + "/trades?" + urllib.parse.urlencode(params)
        data, err = get_json(url)
        if err:
            return count, has_near, first_rows, err
        if not isinstance(data, list):
            return count, has_near, first_rows, "not-list"
        if not data:
            return count, has_near, first_rows, None
        count += len(data)
        # Sohranyaem pervye 3 dlya obraztsa
        if len(first_rows) < 3:
            first_rows.extend(data[:3 - len(first_rows)])
        # Proveryaem blizost k gameStart na letu
        if gst_ts is not None and not has_near:
            for r in data:
                if not isinstance(r, dict):
                    continue
                ts_val = r.get("timestamp") or r.get("time") or r.get("matchTime")
                if ts_val is None:
                    continue
                try:
                    t = float(ts_val)
                    if t > 1e12:
                        t = t / 1000.0
                    delta = gst_ts - t
                    if 0 <= delta <= window_min_s:
                        has_near = True
                        break
                except (TypeError, ValueError):
                    continue
        if len(data) < limit:
            return count, has_near, first_rows, None
        offset += limit
        pages += 1
    return count, has_near, first_rows, "cap-60pages"


def pick_samples(singles, months_needed):
    by_month = defaultdict(list)
    for s in singles:
        by_month[month_key(s["gameStartTime_dt"])].append(s)
    samples = {}
    for mk in months_needed:
        lst = sorted(by_month.get(mk, []), key=lambda x: x["gameStartTime_dt"])
        samples[mk] = lst[:3]
    return samples


# ─── Aggregation helpers (novyy kod dlya razbivki po tigram) ─────

def median_stats(counts):
    """Vozvraschaet dict s median, n_markets, n_with_trades, min, max."""
    if not counts:
        return {"median": 0, "n_markets": 0, "n_with_trades": 0, "min": 0, "max": 0}
    return {
        "median": statistics.median(counts),
        "n_markets": len(counts),
        "n_with_trades": sum(1 for c in counts if c > 0),
        "min": min(counts),
        "max": max(counts),
    }


def aggregate_trades(per_market_rows):
    """Gruppirovka n_trades po (month, tier), po tier, ATP+WTA vmeste, i vsyo.

    Klyuch: (month, tier) --chemy schetchiki ne smeshivayutsya po tigram.
    """
    by_month_tier = defaultdict(list)     # (mk, tier) -> [n_trades, ...]
    by_tier = defaultdict(list)           # tier         -> [n_trades, ...]
    by_month_atp_wta = defaultdict(list)  # mk           -> [n_trades, ...]

    for r in per_market_rows:
        mk = r["month"]
        tier = r["tier"]
        n = r["n_trades"]
        by_month_tier[(mk, tier)].append(n)
        by_tier[tier].append(n)
        if tier in ("ATP", "WTA"):
            by_month_atp_wta[mk].append(n)

    return {
        "by_month_tier": by_month_tier,
        "by_tier": by_tier,
        "by_month_atp_wta": by_month_atp_wta,
        "atp_wta_all": by_tier.get("ATP", []) + by_tier.get("WTA", []),
        "all": [r["n_trades"] for r in per_market_rows],
    }


def aggregate_near_gs(per_market_rows):
    """Dolya rynkov s predmatchevoy sdelkoy (60 min) po tier i ATP+WTA."""
    counters = defaultdict(lambda: {"n_near": 0, "n_total": 0})
    for r in per_market_rows:
        tier = r["tier"]
        counters[tier]["n_total"] += 1
        if r["has_prestart_trade"]:
            counters[tier]["n_near"] += 1
        if tier in ("ATP", "WTA"):
            counters["ATP+WTA"]["n_total"] += 1
            if r["has_prestart_trade"]:
                counters["ATP+WTA"]["n_near"] += 1
        counters["ALL"]["n_total"] += 1
        if r["has_prestart_trade"]:
            counters["ALL"]["n_near"] += 1
    return counters


def select_scan_set(singles):
    """ATP+WTA polnostyu, ITF -- vyborochno (ITF_SAMPLE_SIZE, zerno ITF_SAMPLE_SEED).

    Vozvraschaet (scan_set, itf_total, itf_sampled_n).
    drugie tery (OTHER) -- polnostyu (redkie).
    """
    atp = [s for s in singles if s["tier"] == "ATP"]
    wta = [s for s in singles if s["tier"] == "WTA"]
    itf = [s for s in singles if s["tier"] == "ITF"]
    other = [s for s in singles if s["tier"] == "OTHER"]

    rng = random.Random(ITF_SAMPLE_SEED)
    if len(itf) > ITF_SAMPLE_SIZE:
        itf_sample = rng.sample(itf, ITF_SAMPLE_SIZE)
    else:
        itf_sample = list(itf)

    scan_set = atp + wta + itf_sample + other
    return scan_set, len(itf), len(itf_sample)


# ─── Scan ────────────────────────────────────────────────────────

def scan_trades_parallel(singles, label_prefix=""):
    """Parallel skan /trades po vsem rynkam.

    Klyuch schetchikov -- para (month, tier), ne month.
    Vozvraschaet:
      per_market_rows: list[{slug, tier, month, n_trades, has_prestart_trade}]
      near_gs:          int (chislo rynkov so sdelkoy 60 min do gameStart)
      errors:           int
    """
    errors = 0
    counter = {"done": 0}
    counter_lock = threading.Lock()
    t0 = time.time()
    results_map = {}

    def worker(idx, s):
        try:
            cond = s["conditionId"]
            gst_dt = s["gameStartTime_dt"]
            n, has_near, _first, err = fetch_trades_stats(cond, gst_dt)
            return idx, n, err, has_near
        except Exception as e:
            return idx, 0, "%s: %s" % (type(e).__name__, e), False

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(worker, i, s): i for i, s in enumerate(singles)}
        for fut in as_completed(futs):
            idx, n, err, has_near = fut.result()
            results_map[idx] = (n, err, has_near)
            with counter_lock:
                counter["done"] += 1
                done = counter["done"]
            if done % 500 == 0:
                el = time.time() - t0
                print("    %s%d/%d | %.1f/s" % (label_prefix, done, len(singles), done / el if el else 0))

    per_market_rows = []
    near_gs = 0
    for i, s in enumerate(singles):
        n, err, has_near = results_map[i]
        if err:
            errors += 1
        if has_near:
            near_gs += 1
        per_market_rows.append({
            "slug": s["slug"],
            "tier": s["tier"],
            "month": month_key(s["gameStartTime_dt"]),
            "n_trades": n,
            "has_prestart_trade": has_near,
        })
    return per_market_rows, near_gs, errors


def run_window(label, w_start, w_end):
    print("\n" + "=" * 72)
    print("OKNO: %s  [%s .. %s]" % (label, iso(w_start), iso(w_end)))
    print("=" * 72)
    f_start = w_start - FETCH_PAD
    f_end = w_end + FETCH_PAD
    print("[1] Zagruzka /events?tag_slug=tennis endDate [%s .. %s]..." % (iso(f_start), iso(f_end)))
    ev_closed, cap1, ok1 = fetch_all_events(f_start, f_end, closed=True)
    ev_open, cap2, ok2 = fetch_all_events(f_start, f_end, closed=False)
    events = ev_closed + ev_open
    print("    events closed=%d open=%d total=%d (cap_hits=%d)" % (len(ev_closed), len(ev_open), len(events), cap1 + cap2))

    print("[2] Izvlechenie odinochnyh matchevyh rynkov s gameStartTime v okne...")
    singles = extract_singles_markets(events, w_start, w_end)
    print("    odinochnyh rynkov v okne: %d" % len(singles))

    by = count_by_month_tier(singles)
    print("\n[3] SCHET PO MESYATSAM x TIRAM:")
    print("    %-8s %6s %6s %6s %6s %8s" % ("month", "ATP", "WTA", "ITF", "OTHER", "TOTAL"))
    for mk in sorted(by):
        row = by[mk]
        print("    %-8s %6d %6d %6d %6d %8d" % (
            mk, row.get("ATP", 0), row.get("WTA", 0), row.get("ITF", 0),
            row.get("OTHER", 0), row.get("TOTAL", 0)))

    months_in_window = sorted(set(month_key(w_start + timedelta(days=i))
                                   for i in range((w_end - w_start).days)))
    print("\n[4] PROB /trades: po 3 matcha na mesyac, SYRYE stroki:")
    samples = pick_samples(singles, months_in_window)
    raw_samples = {}
    for mk in months_in_window:
        picks = samples.get(mk, [])
        print("  --- %s: vybrano %d primerov ---" % (mk, len(picks)))
        mk_raw = []
        for idx, s in enumerate(picks):
            cond = s["conditionId"]
            n, _near, first_rows, err = fetch_trades_stats(cond, s["gameStartTime_dt"])
            print("    %d. slug=%s gst=%s trades=%d %s" % (idx + 1, s["slug"], s["gameStartTime"], n, ("ERR:" + err) if err else ""))
            if first_rows:
                print("      --- syrye stroki (pervye 3) ---")
                for r in first_rows[:3]:
                    print("      " + json.dumps(r, ensure_ascii=False)[:500])
                mk_raw.append({"slug": s["slug"], "conditionId": cond, "gameStartTime": s["gameStartTime"],
                               "n_trades": n, "raw_first3": first_rows[:3]})
            else:
                mk_raw.append({"slug": s["slug"], "conditionId": cond, "gameStartTime": s["gameStartTime"],
                               "n_trades": n, "raw_first3": [], "error": err})
        raw_samples[mk] = mk_raw

    # ── Shag 5: vybor scan-seta i skanirovanie ──────────────────
    scan_set, itf_total, itf_sampled = select_scan_set(singles)
    print("\n[5] Skanirovanie /trades: ATP+WTA polnostyu, ITF vyborochno")
    print("    ATP=%d  WTA=%d  ITF(vsego)=%d  ITF(vyborka)=%d  drugie=%d  SKANIRUEM=%d" % (
        sum(1 for s in singles if s["tier"] == "ATP"),
        sum(1 for s in singles if s["tier"] == "WTA"),
        itf_total, itf_sampled,
        sum(1 for s in singles if s["tier"] == "OTHER"),
        len(scan_set)))
    print("    ITF zerno=%d, razmer=%d" % (ITF_SAMPLE_SEED, ITF_SAMPLE_SIZE))
    per_market_rows, near_gs, errors = scan_trades_parallel(scan_set, label_prefix="    ")

    # Agregaciya
    agg = aggregate_trades(per_market_rows)
    near = aggregate_near_gs(per_market_rows)

    # ── Shag 6: mediana po (mesyac x tier) ──────────────────────
    print("\n[6] MEDIANA SDELOK NA MATCH po (mesyac x tier):")
    medians_month_tier = {}
    all_mks = sorted(set(mk for mk, _t in agg["by_month_tier"]))
    for mk in all_mks:
        for tier in ("ATP", "WTA", "ITF"):
            counts = agg["by_month_tier"].get((mk, tier), [])
            if not counts:
                continue
            st = median_stats(counts)
            suffix = "  ВЫБОРОЧНАЯ n=%d" % ITF_SAMPLE_SIZE if tier == "ITF" and itf_total > itf_sampled else ""
            print("    %s %s: median=%.1f  markets=%d  with_trades=%d  min=%d  max=%d%s" % (
                mk, tier, st["median"], st["n_markets"], st["n_with_trades"],
                st["min"], st["max"], suffix))
            medians_month_tier["%s|%s" % (mk, tier)] = st
        # ATP+WTA vmeste dlya etogo mesyaca
        aw = agg["by_month_atp_wta"].get(mk, [])
        if aw:
            st = median_stats(aw)
            print("    %s ATP+WTA: median=%.1f  markets=%d  with_trades=%d  min=%d  max=%d" % (
                mk, st["median"], st["n_markets"], st["n_with_trades"], st["min"], st["max"]))
            medians_month_tier["%s|ATP+WTA" % mk] = st

    # ── Shag 7: dolya predmatchevyh sdelok po tigram ────────────
    print("\n[7] DOLYA rynkov s sdelkoy v predelah 60 min DO gameStartTime:")
    prestart_stats = {}
    for key in ("ATP", "WTA", "ATP+WTA", "ITF"):
        c = near.get(key, {"n_near": 0, "n_total": 0})
        share = (c["n_near"] / c["n_total"]) if c["n_total"] else 0.0
        suffix = "  ВЫБОРОЧНАЯ n=%d" % ITF_SAMPLE_SIZE if key == "ITF" and itf_total > itf_sampled else ""
        print("    %s: %d / %d = %.1f%%%s" % (key, c["n_near"], c["n_total"], share * 100, suffix))
        prestart_stats[key] = {"n_near": c["n_near"], "n_total": c["n_total"], "share": share}
    c_all = near.get("ALL", {"n_near": 0, "n_total": 0})
    share_all = (c_all["n_near"] / c_all["n_total"]) if c_all["n_total"] else 0.0
    print("    ALL (СМЕШАННАЯ_НЕ_ИСПОЛЬЗОВАТЬ): %d / %d = %.1f%%" % (
        c_all["n_near"], c_all["n_total"], share_all * 100))
    prestart_stats["ALL_СМЕШАННАЯ"] = {"n_near": c_all["n_near"], "n_total": c_all["n_total"], "share": share_all}

    # ── Shag 8: itogi po oknu, razbito po tigram ────────────────
    print("\n[8] ITOR PO OKNU (mediany po tigram):")
    tier_medians = {}
    for key in ("ATP", "WTA", "ATP+WTA", "ITF"):
        counts = agg["atp_wta_all"] if key == "ATP+WTA" else agg["by_tier"].get(key, [])
        st = median_stats(counts)
        suffix = "  ВЫБОРОЧНАЯ n=%d" % ITF_SAMPLE_SIZE if key == "ITF" and itf_total > itf_sampled else ""
        print("    %s: median=%.1f  markets=%d  with_trades=%d  min=%d  max=%d%s" % (
            key, st["median"], st["n_markets"], st["n_with_trades"], st["min"], st["max"], suffix))
        tier_medians[key] = st
    overall = median_stats(agg["all"])
    print("    ALL (СМЕШАННАЯ_НЕ_ИСПОЛЬЗОВАТЬ): median=%.1f  markets=%d  with_trades=%d  min=%d  max=%d" % (
        overall["median"], overall["n_markets"], overall["n_with_trades"], overall["min"], overall["max"]))
    tier_medians["ALL_СМЕШАННАЯ"] = overall
    print("    oshibok /trades: %d" % errors)

    return {
        "label": label,
        "window": [iso(w_start), iso(w_end)],
        "n_events_fetched": len(events),
        "n_singles": len(singles),
        "itf_total": itf_total,
        "itf_sampled": itf_sampled,
        "itf_sample_seed": ITF_SAMPLE_SEED,
        "by_month_tier": {mk: dict(v) for mk, v in by.items()},
        "trades_median_by_month_tier": medians_month_tier,
        "trades_median_by_tier": tier_medians,
        "prestart_trade_by_tier": prestart_stats,
        "per_market_rows": per_market_rows,
        "trade_errors": errors,
        "raw_samples_trades": raw_samples,
    }


def selftest():
    assert SINGLES_RE.search("atp-sinner-alcaraz-2026-07-15")
    assert not SINGLES_RE.search("atp-final")
    assert DOUBLES_RE.search("atp-doubles-x-2026-07-15")
    assert tier_of("atp-sinner-alcaraz-2026-02-15") == "ATP"
    assert tier_of("wta-saba-x-2026-02-15") == "WTA"
    assert tier_of("itf-john-doe-2026-03-01") == "ITF"
    assert tier_of("atp-doubles-x-2026-03-01") == "DOUBLES"
    dt = parse_dt("2026-02-15 10:00:00+00")
    assert dt is not None and dt.year == 2026 and dt.month == 2
    gst = datetime(2026, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
    # Test inline near-gamestart logic (same as fetch_trades_stats)
    def _near(rows, gst_dt, window_min_s=3600):
        gst_ts = gst_dt.timestamp()
        for r in rows:
            ts_val = r.get("timestamp")
            if ts_val is None:
                continue
            t = float(ts_val)
            if t > 1e12:
                t = t / 1000.0
            delta = gst_ts - t
            if 0 <= delta <= window_min_s:
                return True
        return False
    assert _near([{"timestamp": gst.timestamp() - 1800}], gst)
    assert not _near([{"timestamp": gst.timestamp() - 7200}], gst)
    assert not _near([{"timestamp": gst.timestamp() + 100}], gst)

    # --- Novyy test: schetchiki (month, tier) ne smeshivayutsya ---
    rows = [
        {"slug": "atp-a-b-2026-05-01", "tier": "ATP", "month": "2026-05", "n_trades": 10, "has_prestart_trade": True},
        {"slug": "wta-c-d-2026-05-01", "tier": "WTA", "month": "2026-05", "n_trades": 20, "has_prestart_trade": False},
        {"slug": "atp-e-f-2026-05-02", "tier": "ATP", "month": "2026-05", "n_trades": 30, "has_prestart_trade": True},
        {"slug": "wta-g-h-2026-06-01", "tier": "WTA", "month": "2026-06", "n_trades": 40, "has_prestart_trade": False},
    ]
    agg = aggregate_trades(rows)
    # ATP v mae tolko 10 i 30, WTA ne primeshalas
    assert agg["by_month_tier"][("2026-05", "ATP")] == [10, 30], \
        "ATP counts smeshalis s WTA: %s" % agg["by_month_tier"][("2026-05", "ATP")]
    assert agg["by_month_tier"][("2026-05", "WTA")] == [20]
    # Po tier: ATP = [10,30], WTA = [20,40]
    assert agg["by_tier"]["ATP"] == [10, 30]
    assert agg["by_tier"]["WTA"] == [20, 40]
    # ATP+WTA po mesyacam: may = [10,20,30], iyun = [40]
    assert agg["by_month_atp_wta"]["2026-05"] == [10, 20, 30]
    assert agg["by_month_atp_wta"]["2026-06"] == [40]
    # ATP+WTA vse = ATP + WTA
    assert sorted(agg["atp_wta_all"]) == [10, 20, 30, 40]
    # near_gs po tier
    near = aggregate_near_gs(rows)
    assert near["ATP"] == {"n_near": 2, "n_total": 2}
    assert near["WTA"] == {"n_near": 0, "n_total": 2}
    assert near["ATP+WTA"] == {"n_near": 2, "n_total": 4}
    assert near["ALL"] == {"n_near": 2, "n_total": 4}

    # ITF vyborka determinirovana i ne prevyshaet ITF_SAMPLE_SIZE
    fake = [{"tier": "ITF", "slug": "itf-%d-2026-05-01" % i,
             "gameStartTime_dt": datetime(2026, 5, 1, tzinfo=timezone.utc)} for i in range(500)]
    _scan, it, isn = select_scan_set(fake)
    assert it == 500 and isn == ITF_SAMPLE_SIZE, "ITF sample size: %d" % isn
    # Povtornyy zapusk daet tot zhe nabor (determinirovannost zerna)
    _scan2, _, _ = select_scan_set(fake)
    s1 = sorted(s["slug"] for s in _scan if s["tier"] == "ITF")
    s2 = sorted(s["slug"] for s in _scan2 if s["tier"] == "ITF")
    assert s1 == s2, "ITF vyborka nedeterminirovana"

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


def main():
    args = sys.argv[1:]
    if args and args[0] == "selftest":
        selftest()
        return

    # Razbor --window {target|current|both}
    window_mode = "both"
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--window":
            if i + 1 >= len(args):
                print("ERROR: --window trebuet znachenie (target|current|both)")
                sys.exit(1)
            window_mode = args[i + 1]
            i += 2
        elif a.startswith("--window="):
            window_mode = a.split("=", 1)[1]
            i += 1
        else:
            print("ERROR: neizvestnyy argument: %s" % a)
            sys.exit(1)

    if window_mode not in ("target", "current", "both"):
        print("ERROR: --window=%s -- dopustimo: target|current|both" % window_mode)
        sys.exit(1)

    open(RAWLOG, "w", encoding="utf-8").close()
    print("GLM PROBA 1 -- tennis window probe (parallel)")
    print("GAMMA:", GAMMA, "| DATA:", DATA, "| workers:", WORKERS, "| rate:", RATE_MAX, "/10s")
    print("Log:", RAWLOG)
    print("Window:", window_mode)

    summary = {"generated": iso(datetime.now(timezone.utc)), "window_mode": window_mode}

    if window_mode in ("target", "both"):
        summary["target"] = run_window("CELEVOE (fevral-aprel 2026)", *WINDOW_TARGET)
    if window_mode in ("current", "both"):
        summary["current"] = run_window("TEKUSCHEE (may-iyul 2026)", *WINDOW_CURRENT)

    if window_mode == "both":
        target = summary["target"]
        current = summary["current"]
        # Granica 28.04
        print("\n" + "=" * 72)
        print("PROVERKA GRANICY 28.04.2026 (CLOB V2 start)")
        print("=" * 72)
        by = target.get("by_month_tier", {})
        for mk in sorted(by):
            print("    %s: total=%d" % (mk, by[mk].get("TOTAL", 0)))
        print("  Granica CLOB V2 = 2026-04-28. Sravnite median(february) vs median(may).")

        print("\n" + "=" * 72)
        print("SRAVNENIE OKON (mediany po tigram)")
        print("=" * 72)
        for tier_key in ("ATP", "WTA", "ATP+WTA", "ITF"):
            tm = target.get("trades_median_by_tier", {}).get(tier_key, {})
            cm = current.get("trades_median_by_tier", {}).get(tier_key, {})
            print("  %-8s celevoe: median=%.1f (n=%d)  tekuschee: median=%.1f (n=%d)" % (
                tier_key, tm.get("median", 0), tm.get("n_markets", 0),
                cm.get("median", 0), cm.get("n_markets", 0)))

    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str, ensure_ascii=False)
    print("\nItog ->", SUMMARY)
    print("Syroy log ->", RAWLOG)


if __name__ == "__main__":
    main()