"""Задача 4: сверялка нашего /book-опроса с внешним эталоном pmdata.dev.

Вход: наш parquet в замороженной схеме (book_poller) и файл pmdata
для того же слага (poly_l2). Слага в наших токенах нет -- Up/Down
токены определяются по gamma (порядок clobTokenIds совпадает с
порядком outcomes ["Up","Down"]).

Выравнивание: по ts_server_ms. Их timestamp в МИКРОсекундах -> приводим
к миллисекундам ЯВНЫМ делением на 1000 (автоприведение запрещено).

Сторона Down приводится к их данным по правилу, подтверждённому в
задачах 3.5/3.6 (pmdata пишет сторону Up):
  цена:   bid_down = 1 - ask_up, ask_down = 1 - bid_up
  размер: bid_size_down = ask_size_up, ask_size_down = bid_size_up
  спред и суммарная глубина при комплементе инвариантны.

Два уровня сравнения, оба обязательны:
  L1 лучшие цены: best_bid, best_ask, spread (их book-события хранят
     цены NULL -- брать из массивов bid_prices[0]/ask_prices[0]).
  L2 глубина: их price_change меняет ОДИН уровень (pc_price/pc_size/
     pc_side), а не всю книгу. Книга восстанавливается: событие book
     как основа + применение дельт. После этого сравнимы vwap_bid_100
     и vwap_ask_100.
"""

from __future__ import annotations

import bisect
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

TICK = 0.01  # один шаг цены на up/down крипто-рынке (порог расхождения)
UP_SIDE = "Up"
DOWN_SIDE = "Down"
GAMMA_URL = "https://gamma-api.polymarket.com"


@dataclass
class BookLevels:
    """Восстановленная книга: цена -> размер, пустые стороны допустимы."""

    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)

    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        price = max(self.bids)
        return (price, self.bids[price])

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        price = min(self.asks)
        return (price, self.asks[price])

    def vwap(self, side: str, quantity: float = 100.0) -> float | None:
        """VWAP первых `quantity` контрактов стороны (None если глубины мало)."""
        levels = self.bids if side == "bid" else self.asks
        if not levels:
            return None
        ordered = sorted(levels.items(), key=lambda kv: kv[0], reverse=(side == "bid"))
        num = 0.0
        total = 0.0
        remaining = quantity
        for price, size in ordered:
            if size <= 0:
                continue
            take = min(remaining, size)
            num += price * take
            total += take
            remaining -= take
            if remaining <= 0:
                return num / total
        return None


@dataclass(frozen=True)
class PMRow:
    """Одна строка pmdata, нормализованная под реконструкцию."""

    ts_ms: int
    event_type: str
    bid_prices: list[float]
    bid_sizes: list[float]
    ask_prices: list[float]
    ask_sizes: list[float]
    pc_price: float | None
    pc_size: float | None
    pc_side: str | None


class BookReconstructor:
    """Реконструкция книги pmdata из (book-основа + price_change-дельты).

    Переиспользуемая: тот же алгоритм нужен коллектору для восстановления
    книги из дельт WS. Состояние инкрементальное, O(1) на дельту.
    """

    def __init__(self, rows: Sequence[PMRow] | None = None) -> None:
        self._rows: list[PMRow] = sorted(
            rows or [], key=lambda r: r.ts_ms
        )
        self._pos = 0
        self._book = BookLevels()

    def add(self, row: PMRow) -> None:
        self._rows.append(row)

    def seal(self) -> None:
        self._rows.sort(key=lambda r: r.ts_ms)

    def _apply(self, row: PMRow) -> None:
        if row.event_type == "book":
            self._book = BookLevels(
                bids=dict(zip(row.bid_prices, row.bid_sizes)),
                asks=dict(zip(row.ask_prices, row.ask_sizes)),
            )
        elif row.event_type == "price_change":
            if row.pc_price is None:
                return
            side = "bid" if row.pc_side == "BUY" else "ask"
            levels = self._book.bids if side == "bid" else self._book.asks
            if row.pc_size is None or row.pc_size <= 0:
                levels.pop(row.pc_price, None)
            else:
                levels[row.pc_price] = row.pc_size
        # market_resolved и прочие типы не меняют книгу.

    def at_or_before(self, ts_ms: int) -> BookLevels:
        """Книга на момент `ts_ms`: применить все события с ts <= ts_ms."""
        while self._pos < len(self._rows) and self._rows[self._pos].ts_ms <= ts_ms:
            self._apply(self._rows[self._pos])
            self._pos += 1
        return self._book


def load_pm_rows(path: Path) -> list[PMRow]:
    """pmdata parquet -> список PMRow (timestamp мкс -> мс ЯВНЫМ делением)."""
    table = pq.read_table(path)
    ts_us = table.column("timestamp").cast(pa.int64()).to_pylist()
    pyl = table.to_pylist()
    rows: list[PMRow] = []
    for i, r in enumerate(pyl):
        rows.append(
            PMRow(
                ts_ms=ts_us[i] // 1000,
                event_type=r.get("event_type") or "",
                bid_prices=[float(x) for x in (r.get("bid_prices") or [])],
                bid_sizes=[float(x) for x in (r.get("bid_sizes") or [])],
                ask_prices=[float(x) for x in (r.get("ask_prices") or [])],
                ask_sizes=[float(x) for x in (r.get("ask_sizes") or [])],
                pc_price=(
                    float(r["pc_price"]) if r.get("pc_price") is not None else None
                ),
                pc_size=float(r["pc_size"]) if r.get("pc_size") is not None else None,
                pc_side=r.get("pc_side"),
            )
        )
    return rows


def down_transform(
    bb_up: float | None, ba_up: float | None, bd: BookLevels | None = None
) -> tuple[float | None, float | None, BookLevels | None]:
    """Комплемент Up -> Down: цены 1-x, размеры со сменой сторон.

    Returns:
        (bid_down, ask_down, transformed_book): если bd задан, возвращается
        книга Down (bid_size_down = ask_size_up и наоборот), иначе None.
    """
    bid_down = (1.0 - ba_up) if ba_up is not None else None
    ask_down = (1.0 - bb_up) if bb_up is not None else None
    book_down = None
    if bd is not None:
        book_down = BookLevels(
            bids={1.0 - p: s for p, s in bd.asks.items()},
            asks={1.0 - p: s for p, s in bd.bids.items()},
        )
    return bid_down, ask_down, book_down


@dataclass
class Metric:
    name: str
    exact: int = 0
    n: int = 0
    abs_diff: list[float] = field(default_factory=list)
    over_tick: int = 0

    def add(self, ours: float | None, theirs: float | None) -> None:
        if ours is None or theirs is None:
            return
        self.n += 1
        d = abs(ours - theirs)
        self.abs_diff.append(d)
        if d <= TICK:
            self.exact += 1
        else:
            self.over_tick += 1

    def report(self) -> dict[str, Any]:
        if not self.abs_diff:
            return {
                "name": self.name,
                "matched": self.n,
                "exact_share": None,
                "median": None,
                "p99": None,
                "max": None,
                "over_tick": 0,
            }
        s = sorted(self.abs_diff)
        return {
            "name": self.name,
            "matched": self.n,
            "exact_share": round(self.exact / self.n, 4),
            "median": round(s[len(s) // 2], 6),
            "p99": round(s[min(len(s) - 1, int(0.99 * len(s)))], 6),
            "max": round(s[-1], 6),
            "over_tick": self.over_tick,
        }


@dataclass
class Mismatch:
    metric: str
    ts_server_ms: int
    ours: float
    theirs: float


def compare_side(
    *,
    rows_pm: Sequence[PMRow],
    snapshots: Sequence[tuple[int, float | None, float | None, float | None, float | None]],
    side_is_down: bool,
    vwap_qty: float = 100.0,
) -> tuple[dict[str, Any], list[Mismatch]]:
    """Сравнение одной стороны (Up или Down) по двум уровням.

    snapshots -- (ts_server_ms, best_bid, best_ask, vwap_bid_100, vwap_ask_100)
    по одному на наш снимок. side_is_down=True -> комплемент к их данным.
    """
    rec = BookReconstructor(rows_pm)
    metrics = {
        "best_bid": Metric("best_bid"),
        "best_ask": Metric("best_ask"),
        "spread": Metric("spread"),
        "vwap_bid_100": Metric("vwap_bid_100"),
        "vwap_ask_100": Metric("vwap_ask_100"),
    }
    mismatches: list[Mismatch] = []

    for ts, our_bid, our_ask, our_vbid, our_vask in snapshots:
        book = rec.at_or_before(ts)
        if side_is_down:
            their_bid, their_ask, dbook = down_transform(
                book.best_bid()[0] if book.best_bid() else None,
                book.best_ask()[0] if book.best_ask() else None,
                book,
            )
            their_vbid = dbook.vwap("bid", vwap_qty) if dbook else None
            their_vask = dbook.vwap("ask", vwap_qty) if dbook else None
        else:
            bb = book.best_bid()
            ba = book.best_ask()
            their_bid = bb[0] if bb else None
            their_ask = ba[0] if ba else None
            their_vbid = book.vwap("bid", vwap_qty)
            their_vask = book.vwap("ask", vwap_qty)

        checks = [
            ("best_bid", our_bid, their_bid),
            ("best_ask", our_ask, their_ask),
        ]
        spread_ours = (our_ask - our_bid) if (our_bid is not None and our_ask is not None) else None
        spread_theirs = (
            (their_ask - their_bid)
            if (their_bid is not None and their_ask is not None)
            else None
        )
        checks.append(("spread", spread_ours, spread_theirs))
        checks.append(("vwap_bid_100", our_vbid, their_vbid))
        checks.append(("vwap_ask_100", our_vask, their_vask))

        for name, ours, theirs in checks:
            m = metrics[name]
            m.add(ours, theirs)
            if ours is not None and theirs is not None and abs(ours - theirs) > TICK:
                mismatches.append(Mismatch(name, ts, ours, theirs))

    report = {name: m.report() for name, m in metrics.items()}
    return report, mismatches


def load_our_rows(path: Path) -> list[dict[str, Any]]:
    """Наш parquet -> список строк (как записаны)."""
    table = pq.read_table(path)
    return table.to_pylist()


def write_result(
    path: Path,
    *,
    our_path: str,
    pm_path: str,
    slug: str,
    mapping: dict[str, str],
    matched: dict[str, dict[str, int]],
    reports: dict[str, dict[str, Any]],
    mismatches: dict[str, list[Mismatch]],
    unmatched: dict[str, int],
    dropped_null: dict[str, int],
) -> None:
    """Файл результата сверки (JSON, UTF-8) в data/validate/."""
    payload = {
        "slug": slug,
        "our_file": our_path,
        "pm_file": pm_path,
        "token_side": {k: v for k, v in mapping.items()},
        "unmatched": unmatched,
        "dropped_null": dropped_null,
        "matched_snapshots": matched,
        "per_metric": {
            side: {name: r for name, r in report.items()}
            for side, report in reports.items()
        },
        "mismatches_over_tick": {
            side: [
                {
                    "metric": m.metric,
                    "ts_server_ms": m.ts_server_ms,
                    "ours": m.ours,
                    "theirs": m.theirs,
                }
                for m in mm[:10]
            ]
            for side, mm in mismatches.items()
        },
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def token_side_from_gamma(client: httpx.Client, slug: str) -> dict[str, str]:
    """token_id -> "Up"/"Down" по порядку clobTokenIds в gamma.

    Эпоха в слаге up/down -- конец окна; резолв наступает через ~15-30 мин,
    endDate события = время резолва. Ищем узким окном вокруг резолва, чтобы
    не упираться в offset-потолок (≈2000 событий на широком окне).
    """
    try:
        epoch = int(slug.rsplit("-", 1)[1])
    except (ValueError, IndexError) as exc:
        raise RuntimeError(f"не удалось извлечь эпоху из слага {slug!r}") from exc
    lo = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch - 1800))
    hi = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch + 3600))
    for offset in (0, 1000, 2000):
        r = client.get(
            GAMMA_URL + "/events",
            params={
                "tag_slug": "crypto",
                "end_date_min": lo,
                "end_date_max": hi,
                "limit": 100,
                "offset": offset,
            },
            timeout=60,
        )
        r.raise_for_status()
        for event in r.json():
            for m in event.get("markets", []):
                if m.get("slug") == slug:
                    tokens = json.loads(m.get("clobTokenIds") or "[]")
                    outcomes = json.loads(m.get("outcomes") or "[]")
                    mapping = dict(zip(tokens, outcomes))
                    return mapping
    raise RuntimeError(f"слаг {slug} не найден в gamma в окне [{lo}, {hi}]")


def main(argv: Sequence[str] | None = None) -> int:
    our_path = Path(argv[1] if argv and len(argv) > 1 else "data/validate/book_poll_15m_20260802T064911Z.parquet")
    pm_path = Path(argv[2] if argv and len(argv) > 2 else "data/validate/btc-updown-15m-1785653100.parquet")
    slug = argv[3] if argv and len(argv) > 3 else "btc-updown-15m-1785653100"

    rows_pm = load_pm_rows(pm_path)
    print(f"pm rows: {len(rows_pm)}; range_ts_ms: {rows_pm[0].ts_ms}..{rows_pm[-1].ts_ms}")

    with httpx.Client(base_url=GAMMA_URL, timeout=30.0) as client:
        mapping = token_side_from_gamma(client, slug)
    print(f"token->side ({slug}):", {k[:12]: v for k, v in mapping.items()})

    ours = load_our_rows(our_path)
    ours_by_side: dict[str, list[tuple[int, float | None, float | None, float | None, float | None]]] = {
        UP_SIDE: [], DOWN_SIDE: []
    }
    unmatched = {"no_token": 0, "null_prices": 0}
    for row in ours:
        tok = row["token_id"]
        side = mapping.get(tok)
        if side is None:
            unmatched["no_token"] += 1
            continue
        if row["ts_server_ms"] is None:
            unmatched["null_prices"] += 1
            continue
        if row["best_bid"] is None and row["best_ask"] is None:
            unmatched["null_prices"] += 1
            continue
        ours_by_side[side].append(
            (
                row["ts_server_ms"],
                row["best_bid"],
                row["best_ask"],
                row["vwap_bid_100"],
                row["vwap_ask_100"],
            )
        )
    print(f"снимков всего: {len(ours)}; без пары: {sum(unmatched.values())} "
          f"({unmatched}); Up: {len(ours_by_side[UP_SIDE])}, "
          f"Down: {len(ours_by_side[DOWN_SIDE])}")

    dropped_null: dict[str, int] = {}
    for side, snaps in ours_by_side.items():
        dropped_null[side] = sum(
            1
            for (_ts, our_bid, our_ask, _vb, _va) in snaps
            if our_bid is None or our_ask is None
        )

    reports: dict[str, dict[str, Any]] = {}
    mismatches_all: dict[str, list[Mismatch]] = {}
    for side in (UP_SIDE, DOWN_SIDE):
        snaps = ours_by_side[side]
        if not snaps:
            print(f"\n=== {side}: снимков нет")
            continue
        report, mismatches = compare_side(
            rows_pm=rows_pm,
            snapshots=snaps,
            side_is_down=(side == DOWN_SIDE),
        )
        reports[side] = report
        mismatches_all[side] = mismatches
        print(f"\n=== {side} ===")
        for mname, r in report.items():
            print(
                f"  {mname:14s} matched={r['matched']:4d} exact_share={r['exact_share']} "
                f"med={r['median']} p99={r['p99']} max={r['max']} over_tick={r['over_tick']}"
            )
        print(f"  расхождений > 1 tick: {sum(r['over_tick'] for r in report.values())}")
        print(f"  отброшено из-за NULL цен: {dropped_null[side]} из {len(snaps)}")
        first10 = mismatches[:10]
        print(f"  первых десять (всего {len(mismatches)}):")
        for mm in first10:
            print(
                f"    {mm.metric:12s} ts={mm.ts_server_ms} "
                f"ours={mm.ours:.6f} theirs={mm.theirs:.6f} |d|={abs(mm.ours-mm.theirs):.6f}"
            )

    matched = {
        side: {name: r["matched"] for name, r in report.items()}
        for side, report in reports.items()
    }
    out = Path("data/validate") / f"compare_{slug}.json"
    write_result(
        out,
        our_path=str(our_path),
        pm_path=str(pm_path),
        slug=slug,
        mapping=mapping,
        matched=matched,
        reports=reports,
        mismatches=mismatches_all,
        unmatched=unmatched,
        dropped_null=dropped_null,
    )
    print(f"\nрезультат: {out} (отброшено NULL: {dropped_null})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
