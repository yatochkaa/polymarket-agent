# diag_trades_depth.py -- odin rynok: glubina /trades?market= pri bolshom limit
# + ponimaet li endpoint sortirovku po vozrastaniyu. Bez lukapa spiska rynkov
# (imenno on dal IndexError: ms[] pust). Cond zahardkozhen -> nichego ne indeksiruem vslepuyu.
# Zapusk (PS): python -u probes\diag_trades_depth.py
import json, urllib.request, urllib.parse, urllib.error
import datetime as dt

DATA  = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
COND  = "0xb1770d16789541de8c805397d2012e1f2e2ff211757098f9df6acbe43540a085"
SLUG  = "wta-osorio-cristia-2026-02-01"
UA    = {"User-Agent": "Mozilla/5.0 (depth-probe)"}

def get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode("utf-8"))

def iso(ts):
    try:
        return dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except Exception:
        return str(ts)

def ts_of(row):
    if not isinstance(row, dict):
        return None
    for k in ("timestamp", "time", "t"):
        if k in row:
            try:
                return int(row[k])
            except Exception:
                pass
    return None

def probe(limit, offset=0, extra=None):
    q = {"market": COND, "limit": limit, "offset": offset}
    if extra:
        q.update(extra)
    url = DATA + "/trades?" + urllib.parse.urlencode(q)
    tag = (" +" + urllib.parse.urlencode(extra)) if extra else ""
    try:
        rows = get(url)
    except urllib.error.HTTPError as e:
        print("  limit=%-6s offset=%-6s%s -> HTTP %s" % (limit, offset, tag, e.code))
        return None
    except Exception as e:
        print("  limit=%-6s offset=%-6s%s -> ERR %s" % (limit, offset, tag, e))
        return None
    if not isinstance(rows, list):
        print("  limit=%-6s offset=%-6s%s -> NE spisok: %r" % (limit, offset, tag, rows))
        return None
    n = len(rows)
    f = ts_of(rows[0]) if n > 0 else None
    l = ts_of(rows[-1]) if n > 0 else None
    if f is not None and l is not None:
        order = "DESC(novye->starye)" if f >= l else "ASC(starye->novye)"
    else:
        order = "?"
    print("  limit=%-6s offset=%-6s%s -> rows=%d  first_ts=%s  last_ts=%s  %s"
          % (limit, offset, tag, n, iso(f) if f else "-", iso(l) if l else "-", order))
    return rows

print("=== COND %s ===" % COND)
print("=== SLUG %s ===" % SLUG)

print("\n=== GLUBINA: odin zapros, offset=0, raznyy limit ===")
print("    (esli rows > 10000 -> bolshoy limit rabotaet, hvost chitaetsya odnim zaprosom)")
for lim in (1000, 10000, 20000, 30000, 50000):
    probe(lim, 0)

print("\n=== OFFSET pri limit=10000 (offset>10000 dolzhen dat' HTTP 400) ===")
for off in (0, 10000, 11000, 20000):
    probe(10000, off)

print("\n=== SORTIROVKA: probuem kandidatov-parametrov, sravnivaem first_ts/last_ts ===")
print("    (bazovyy bez parametra dolzhen byt' DESC; esli kakoy-to param dast ASC -> hvost s pervoy stranicy)")
probe(1000, 0)
for extra in ({"ascending": "true"},
              {"order": "timestamp", "ascending": "true"},
              {"order": "timestamp"},
              {"sortDirection": "ASC"},
              {"sort": "timestamp", "dir": "asc"}):
    probe(1000, 0, extra)

print("\n=== gameStartTime (spravochno, NE fatalno) ===")
try:
    ev = get(GAMMA + "/markets?slug=" + urllib.parse.quote(SLUG))
    if isinstance(ev, list) and ev:
        m0 = ev[0]
        print("  gameStartTime=%s  startDate=%s" % (m0.get("gameStartTime"), m0.get("startDate")))
    else:
        print("  markets?slug= vernul pusto -> sravnivay first/last ts s 2026-02-01 vruchnuyu")
except Exception as e:
    print("  gst lookup ERR: %s (ne kritichno dlya voprosa glubiny)" % e)

print("\n=== VYVOD ===")
print("  1) esli limit>10000 dal rows>10000  -> chitaem ves' rynok odnim zaprosom (vetka 2).")
print("  2) inache esli kakoy-to param dal ASC -> predmatch = pervaya stranica (vetka 1).")
print("  3) inache (desc, limit<=10000, offset<=10000) -> predmatch-hvost nedostizhim (vetka 3).")
