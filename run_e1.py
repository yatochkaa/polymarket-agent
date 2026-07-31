"""Раннер живого прогона Э1 (Поправка 2). Запуск из корня репо.

Эталоны: mid из /book, last_trade — реальная сделка из /trades (фильтр по asset).
Выборка рынков — из e1_markets.json (его пишет scout_e1_market.py).

  # смоук пломбировки одного токена (ничего не пишет):
  python -u run_e1.py --token <YES> --cond <cond> --samples 4 --interval 10 --no-write
  # полный прогон по выборке (пишет data/e1_result.json ТОЛЬКО на чистом дереве):
  python -u run_e1.py --markets e1_markets.json --samples 40 --interval 30 --out data/e1_result.json
  # КАЧЕНИЕ по цепочкам коротких up/down (объём захвата = cycles x слоты, фиксирован заранее):
  python -u run_e1.py --rolling --cycles 40 --interval 30 --slots bitcoin,ethereum,solana --warmup 60 --out data/e1_result.json

ГАРАНТИЯ честности (ПОРЯДОК): результат пишется ТОЛЬКО если
  (а) прогон реально отработал, и
  (б) рабочее дерево чистое по отслеживаемым файлам (Поправка 2 закоммичена).
В результат штампуется git HEAD (коммит заморозки). На грязном дереве файл НЕ пишется.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pm.config import load_settings
from pm.httpc import ReadClient
from pm.experiments.e1_prices_history import (
    CurrentMarket,
    MarketRef,
    SlotResolver,
    fetch_book_sample,
    fetch_trades_raw,
    run,
    run_rolling,
    select_token_trades,
)


def _client(base_url: str, s, raw_log: Path | None = None) -> ReadClient:
    return ReadClient(
        base_url,
        timeout_s=s.request_timeout_s,
        max_retries=s.max_retries,
        rate_limit_rps=s.rate_limit_rps,
        raw_log=raw_log,
    )


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=15)
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _tree_dirty() -> bool | None:
    """True если есть изменённые ОТСЛЕЖИВАЕМЫЕ файлы (untracked '??' игнорируются)."""
    st = _git("status", "--porcelain")
    if st is None:
        return None
    for line in st.splitlines():
        if line and not line.startswith("??"):
            return True
    return False


def _load_markets(args) -> list[MarketRef]:
    if args.markets:
        data = json.loads(Path(args.markets).read_text(encoding="utf-8"))
        return [MarketRef(token_id=str(r["token_id"]), condition_id=str(r["condition_id"]), slug=r.get("slug")) for r in data]
    if args.token and args.cond:
        return [MarketRef(token_id=args.token, condition_id=args.cond, slug=None)]
    raise SystemExit("укажите --markets e1_markets.json ЛИБО --token и --cond")


_SLUG_RE = re.compile(r"updown-(\d+)(m|h)-(\d{9,11})")


def _binary_token(m: dict) -> str | None:
    """token id ноги Up/Yes для строго бинарного рынка (2 исхода, 2 токена, сумма цен ~1)."""
    try:
        toks = json.loads(m.get("clobTokenIds") or "[]")
        outs = json.loads(m.get("outcomes") or "[]")
    except (TypeError, ValueError):
        return None
    if not (isinstance(toks, list) and isinstance(outs, list) and len(toks) == 2 and len(outs) == 2):
        return None
    prices = m.get("outcomePrices")
    try:
        pr = json.loads(prices) if isinstance(prices, str) else prices
        ssum = sum(float(x) for x in pr) if pr else None
    except (TypeError, ValueError):
        ssum = None
    if ssum is None or abs(ssum - 1.0) > 0.02:
        return None
    return str(toks[0])


def _events_list(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("data") or payload.get("events") or payload.get("results") or []
    return []


def _live_updown(gamma: ReadClient, tag: str):
    """Текущий живой короткий up/down рынок актива (epoch<=now<epoch+dur,
    active&acceptingOrders&not closed, бинарный). Из нескольких — самый короткий
    по длительности (макс. активность), при равенстве — самый поздний по старту.
    Возвращает CurrentMarket или None."""
    now = time.time()
    try:
        env = gamma.get("/events", {"tag_slug": tag, "closed": "false", "limit": 500})
    except Exception:  # noqa: BLE001
        return None
    best_key = None
    best_cm = None
    for ev in _events_list(env.payload):
        if not isinstance(ev, dict):
            continue
        for m in ev.get("markets") or []:
            if not isinstance(m, dict):
                continue
            slug = m.get("slug") or ""
            mm = _SLUG_RE.search(slug)
            if not mm:
                continue
            n = int(mm.group(1))
            dur = n * 60 if mm.group(2) == "m" else n * 3600
            ep = int(mm.group(3))
            if not (ep <= now < ep + dur):
                continue
            if not (m.get("active") and not m.get("closed") and m.get("acceptingOrders")):
                continue
            tok = _binary_token(m)
            if tok is None:
                continue
            cond = str(m.get("conditionId") or "")
            if not cond:
                continue
            key = (dur, -ep)
            if best_key is None or key < best_key:
                best_key = key
                best_cm = CurrentMarket(token_id=tok, condition_id=cond, slug=slug, start_ts=ep)
    return best_cm


class ChainResolver(SlotResolver):
    """Слот-цепочка одного актива: каждый current() заново выбирает живой рынок."""

    def __init__(self, gamma: ReadClient, tag: str):
        self.key = tag
        self._gamma = gamma

    def current(self):
        return _live_updown(self._gamma, self.key)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", default=None, help="JSON выборки от scout (список token_id/condition_id/slug)")
    ap.add_argument("--token", default=None, help="один YES token id (для смоука)")
    ap.add_argument("--cond", default=None, help="один conditionId (для смоука)")
    ap.add_argument("--samples", type=int, default=40)
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--rolling", action="store_true", help="rezhim kacheniya po cepochkam korotkih up/down")
    ap.add_argument("--cycles", type=int, default=None, help="chislo ciklov kacheniya (zaranee fiksiruemyy OBEM zahvata)")
    ap.add_argument("--slots", default="bitcoin,ethereum,solana", help="tegi aktivov-cepochek cherez zapyatuyu")
    ap.add_argument("--warmup", type=int, default=60, help="sek razogreva, isklyuchayutsya iz znamenatelya")
    ap.add_argument("--out", default="data/e1_result.json")
    ap.add_argument("--no-write", action="store_true", help="не писать результат (смоук)")
    args = ap.parse_args()

    s = load_settings()
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    clob = _client(s.clob_host, s, raw_log=logs_dir / "e1_raw.jsonl")
    data = _client(s.data_host, s)

    head = _git("rev-parse", "HEAD")
    if args.rolling:
        if not args.cycles:
            raise SystemExit("--rolling trebuet --cycles N (zaranee fiksiruemyy OBEM zahvata)")
        gamma = _client(s.gamma_host, s)
        tags = [t.strip() for t in args.slots.split(",") if t.strip()]
        resolvers = [ChainResolver(gamma, t) for t in tags]
        plan = args.cycles * len(resolvers)
        eta_min = args.cycles * args.interval / 60.0
        print(f"E1 ROLLING: slots={tags} cycles={args.cycles} interval={args.interval}s snapshots_plan={plan} warmup={args.warmup}s (~{eta_min:.1f} min zahvata)")
        print(f"preregistration_commit(HEAD)={head}")
        cur0 = resolvers[0].current() if resolvers else None
        if cur0 is not None:
            try:
                bs = fetch_book_sample(clob, cur0.token_id)
                raw = fetch_trades_raw(data, cur0.condition_id)
                tr = select_token_trades(raw, cur0.token_id)
                print(f"[pre] {cur0.slug} mid={bs.mid} tick={bs.tick} trades_token={len(tr)} last_real={tr[-1] if tr else None}")
            except Exception as e:  # noqa: BLE001
                print(f"[pre] validaciya ne udalas: {e}")
        else:
            print("[pre] u pervogo slota net zhivogo rynka seychas (resolver -> None)")
        t0 = time.time()
        report = run_rolling(
            s, clob, data, resolvers,
            n_cycles=args.cycles, interval_s=args.interval, warmup_s=args.warmup,
            fidelity=1, preregistration_commit=head,
        )
        dt = time.time() - t0
        gamma.close()
    else:
        markets = _load_markets(args)
        eta_min = args.samples * args.interval / 60.0
        print(f"E1 run: markets={len(markets)} samples={args.samples} interval={args.interval}s (~{eta_min:.1f} min zahvata)")
        print(f"preregistration_commit(HEAD)={head}")

        # --- [pre] валидация эталона на первом рынке (до захвата) ---
        m0 = markets[0]
        try:
            bs = fetch_book_sample(clob, m0.token_id)
            raw = fetch_trades_raw(data, m0.condition_id)
            tr = select_token_trades(raw, m0.token_id)
            last_actual = tr[-1] if tr else None
            print(f"[pre] {m0.slug or m0.token_id[:16]} mid={bs.mid} tick={bs.tick} book.last_trade(IGNORIRUEM)={bs.book_last_trade_price}")
            print(f"[pre] trades_po_tokenu(asset)={len(tr)} poslednyaya_REALNAYA_sdelka(ts,price)={last_actual}")
            if bs.mid is not None and last_actual is not None and bs.tick:
                gap_t = abs(bs.mid - last_actual[1]) / bs.tick
                print(f"[pre] gap mid-vs-REALtrade={gap_t:.1f} tikov (nuzhno stabilno >0.5 dlya divergent)")
        except Exception as e:  # noqa: BLE001
            print(f"[pre] validaciya ne udalas: {e}")

        t0 = time.time()
        report = run(
            s, clob, data, markets,
            n_samples=args.samples, interval_s=args.interval, fidelity=1,
            preregistration_commit=head,
        )
        dt = time.time() - t0

    payload = asdict(report)
    print("")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\nVERDICT={report.verdict}  n_divergent(total)={report.totals.get('n_divergent')}  n_warmup={report.totals.get('n_warmup')}  (zahvat {dt/60:.1f} min)")

    if args.no_write:
        print("REZULTAT ne zapisan (--no-write).")
    else:
        dirty = _tree_dirty()
        if dirty is None:
            print("OTKAZ pisat rezultat: git nedostupen, ne mogu podtverdit zamorozku Popravki 2.")
        elif dirty:
            print("OTKAZ pisat rezultat: rabochee derevo GRYAZNOE (est nezakommichennye otslezhivaemye izmeneniya).")
            print("Zamorozte i zakommitte Popravku 2, potom povtorite progon.")
        else:
            out = Path(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"REZULTAT zapisan: {out}  (preregistration_commit={head})")

    clob.close()
    data.close()
    print("E1_RUN_DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
