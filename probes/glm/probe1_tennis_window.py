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
  3. Mediana chisla sdelok na match po mesyacam.
  4. Dolya rynkov, gde est sdelka v predelah 60 minut do gameStartTime.
  5. Te zhe 4 velichiny za tekuschee okno 2026-05-02 .. 2026-07-31 (sravnenie).
  6. Viden li razryv na granice 28 aprelya.

KLYUCHEVOY NYUANS (ubytyy pri razvedke): event.endDate != match date.
gameStartTime (na urovne RYNKA) -- nastoyaschee vremya matcha. endDate otstaet
do ~2 nedel (rezolv). Poetomu: shirokiy srez po endDate, zatem filtr po
gameStartTime iz samogo rynka.

Potokobezopasnyy throttle (deque) ~140 req/10s, ThreadPoolExecutor dlya /trades.
Vse logi syrye v probes/glm/probe1_*.log. Skript nichego ne chinit.

usage:
  python probes/glm/probe1_tennis_window.py            # polnyy progion
  python probes/glm/probe1_tennis_window.py selftest   # samotest
"""
import json, os, re, sys, time, statistics, threading, collections
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


def scan_trades_parallel(singles, label_prefix=""):
    """Parallel skan /trades po vsem rynkam. Vozvraschaet result dict."""
    per_market = []
    near_gs = 0
    trades_counts_by_month = defaultdict(list)
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
            s = singles[idx]
            results_map[idx] = (n, err, has_near)
            with counter_lock:
                counter["done"] += 1
                done = counter["done"]
            if done % 500 == 0:
                el = time.time() - t0
                print("    %s%d/%d | %.1f/s" % (label_prefix, done, len(singles), done / el if el else 0))

    for i, s in enumerate(singles):
        n, err, has_near = results_map[i]
        if err:
            errors += 1
        per_market.append(n)
        mk = month_key(s["gameStartTime_dt"])
        trades_counts_by_month[mk].append(n)
        if has_near:
            near_gs += 1

    return per_market, near_gs, trades_counts_by_month, errors


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

    print("\n[5] Skanirovanie /trades po VSEM %d odinochnym rynkam (workers=%d)..." % (len(singles), WORKERS))
    per_market, near_gs, trades_counts_by_month, errors = scan_trades_parallel(singles, label_prefix="    ")

    medians = {}
    print("\n[6] MEDIANA SDELOK NA MATCH po mesyatsam:")
    for mk in sorted(trades_counts_by_month):
        counts = trades_counts_by_month[mk]
        med = statistics.median(counts) if counts else 0
        nonzero = sum(1 for c in counts if c > 0)
        medians[mk] = {"median": med, "n_markets": len(counts), "n_with_trades": nonzero,
                       "min": min(counts) if counts else 0, "max": max(counts) if counts else 0}
        print("    %s: median=%.1f  markets=%d  with_trades=%d  min=%d  max=%d" % (
            mk, med, len(counts), nonzero, min(counts) if counts else 0, max(counts) if counts else 0))

    share_near = (near_gs / len(singles)) if singles else 0.0
    print("\n[7] DOLYA rynkov s sdelkoy v predelah 60 min DO gameStartTime:")
    print("    %d / %d = %.1f%%" % (near_gs, len(singles), share_near * 100))

    overall_median = statistics.median(per_market) if per_market else 0
    print("\n[8] ITOR PO OKNU:")
    print("    odinochnyh rynkov: %d" % len(singles))
    print("    mediana sdelok (vse okno): %.1f" % overall_median)
    print("    dolya s predmatchevoy sdelkoy (60 min): %.1f%%" % (share_near * 100))
    print("    oshibok /trades: %d" % errors)

    return {
        "label": label, "window": [iso(w_start), iso(w_end)],
        "n_events_fetched": len(events), "n_singles": len(singles),
        "by_month_tier": {mk: dict(v) for mk, v in by.items()},
        "trades_median_by_month": medians,
        "overall_median_trades": overall_median,
        "share_with_prestart_trade_60min": share_near,
        "n_with_prestart_trade": near_gs,
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
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        selftest()
        return
    open(RAWLOG, "w", encoding="utf-8").close()
    print("GLM PROBA 1 -- tennis window probe (parallel)")
    print("GAMMA:", GAMMA, "| DATA:", DATA, "| workers:", WORKERS, "| rate:", RATE_MAX, "/10s")
    print("Log:", RAWLOG)

    target = run_window("CELEVOE (fevral-aprel 2026)", *WINDOW_TARGET)
    current = run_window("TEKUSCHEE (may-iyul 2026)", *WINDOW_CURRENT)

    # Granica 28.04
    print("\n" + "=" * 72)
    print("PROVERKA GRANICY 28.04.2026 (CLOB V2 start)")
    print("=" * 72)
    by = target.get("by_month_tier", {})
    for mk in sorted(by):
        print("    %s: total=%d" % (mk, by[mk].get("TOTAL", 0)))
    print("  Granica CLOB V2 = 2026-04-28. Sravnite median(february) vs median(may).")

    print("\n" + "=" * 72)
    print("SRAVNENIE OKON")
    print("=" * 72)
    print("  CELEVOE  (feb-apr): rynkov=%d  median=%.1f  dolya_predstart=%.1f%%" % (
        target["n_singles"], target["overall_median_trades"],
        target["share_with_prestart_trade_60min"] * 100))
    print("  TEKUSCHEE (may-jul): rynkov=%d  median=%.1f  dolya_predstart=%.1f%%" % (
        current["n_singles"], current["overall_median_trades"],
        current["share_with_prestart_trade_60min"] * 100))

    summary = {"generated": iso(datetime.now(timezone.utc)), "target": target, "current": current}
    with open(SUMMARY, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, default=str, ensure_ascii=False)
    print("\nItog ->", SUMMARY)
    print("Syroy log ->", RAWLOG)


if __name__ == "__main__":
    main()