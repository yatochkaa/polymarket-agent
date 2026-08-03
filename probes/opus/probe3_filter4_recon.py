#!/usr/bin/env python3
"""Filter 4: compare positions reconstructed from trades with open positions."""
from __future__ import annotations

import json
import re
import sys
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

BASE_GAMMA = "https://gamma-api.polymarket.com"
BASE_DATA = "https://data-api.polymarket.com"
DATE_SUFFIX = re.compile(r"\d{4}-\d{2}-\d{2}$")
DOUBLES = ("atp-doubles-", "wta-doubles-")
EPS = Decimal("0.000001")
PAGE_LIMIT = 1000
MAX_OFFSET = 10000
TRADES_DELAY = 0.055
POSITIONS_DELAY = 0.075


def dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def api_get(base: str, path: str, params: dict[str, Any]) -> Any:
    url = base + path + "?" + urlencode(params)
    req = Request(url, headers={"Accept": "application/json", "User-Agent": "probe3-filter4/1.0"})
    while True:
        try:
            with urlopen(req, timeout=45) as response:
                raw = response.read()
            return json.loads(raw)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            if exc.code == 429:
                time.sleep(1.5)
                continue
            raise RuntimeError(f"HTTP {exc.code} {url}: {body[:500]}") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError(f"request failed {url}: {exc}") from exc


def as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("data", "trades", "positions", "events"):
            if isinstance(value.get(key), list):
                return [x for x in value[key] if isinstance(x, dict)]
    return []


def get_all_trades(params: dict[str, Any], stats: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = as_list(api_get(BASE_DATA, "/trades", {**params, "limit": PAGE_LIMIT, "offset": offset}))
        stats["trade_requests"] += 1
        stats["trade_rows"] += len(page)
        result.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        if offset > MAX_OFFSET:
            stats["hit_offset_cap"] = True
            break
        time.sleep(TRADES_DELAY)
    return result


def get_all_positions(user: str, stats: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = as_list(api_get(BASE_DATA, "/positions", {"user": user, "limit": PAGE_LIMIT, "offset": offset}))
        stats["position_requests"] += 1
        result.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        offset += PAGE_LIMIT
        if offset > MAX_OFFSET:
            stats["hit_position_cap"] = True
            break
        time.sleep(POSITIONS_DELAY)
    return result


def main() -> int:
    stats: dict[str, Any] = defaultdict(int)
    stats["hit_offset_cap"] = False
    stats["hit_position_cap"] = False
    events = as_list(api_get(BASE_GAMMA, "/events", {"tag_slug": "tennis", "closed": "false", "limit": 100}))
    markets: list[dict[str, Any]] = []
    for event in events:
        for market in as_list(event.get("markets")):
            slug = str(market.get("slug") or "")
            if DATE_SUFFIX.search(slug) and not slug.startswith(DOUBLES):
                markets.append(market)
    # Deterministic selection: first three returned by Gamma.
    markets = markets[:3]
    if len(markets) < 3:
        raise RuntimeError(f"only {len(markets)} qualifying open markets returned")

    wallets: list[str] = []
    market_trade_cache: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        condition = str(market.get("conditionId") or "")
        rows = get_all_trades({"market": condition}, stats)
        market_trade_cache[condition] = rows
        for row in rows:
            wallet = str(row.get("proxyWallet") or "").lower()
            if wallet and wallet not in wallets:
                wallets.append(wallet)
            if len(wallets) >= 20:
                break
        if len(wallets) >= 20:
            break
    wallets = wallets[:20]

    checked = 0
    matching = 0
    divergent: list[dict[str, Any]] = []
    positions_without_market_trades = 0
    raw_examples: list[dict[str, Any]] = []

    for wallet in wallets:
        positions = get_all_positions(wallet, stats)
        all_trades = get_all_trades({"user": wallet}, stats)
        time.sleep(TRADES_DELAY)
        trades_by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for trade in all_trades:
            condition = str(trade.get("conditionId") or "")
            trades_by_condition[condition].append(trade)

        for position in positions:
            condition = str(position.get("conditionId") or "")
            if not condition:
                continue
            checked += 1
            asset = str(position.get("asset") or "")
            opposite = str(position.get("oppositeAsset") or "")
            actual = dec(position.get("size"))
            reconstructed = Decimal("0")
            relevant = trades_by_condition.get(condition, [])
            for trade in relevant:
                trade_asset = str(trade.get("asset") or "")
                size = dec(trade.get("size"))
                side = str(trade.get("side") or "").upper()
                if trade_asset == asset:
                    reconstructed += size if side == "BUY" else -size if side == "SELL" else Decimal("0")
                elif opposite and trade_asset == opposite:
                    reconstructed += -size if side == "BUY" else size if side == "SELL" else Decimal("0")
            difference = reconstructed - actual
            if not relevant:
                positions_without_market_trades += 1
            if abs(difference) <= EPS:
                matching += 1
            else:
                divergent.append({
                    "wallet": wallet,
                    "conditionId": condition,
                    "difference": str(difference),
                    "sign": "positive" if difference > 0 else "negative",
                    "magnitude": str(abs(difference)),
                    "position": position,
                    "trade_count_for_market": len(relevant),
                })
                if len(raw_examples) < 3:
                    raw_examples.append({"position": position, "trades": relevant[:3]})

    print(f"wallets={len(wallets)}")
    print(f"wallet_market_pairs_checked={checked}")
    print(f"matching_pairs={matching}")
    print(f"matching_share={(matching / checked if checked else 0):.12f}")
    print(f"divergent_pairs={len(divergent)}")
    for item in divergent:
        print(f"divergence difference={item['difference']} sign={item['sign']} magnitude={item['magnitude']}")
    print(f"positions_without_any_market_trades={positions_without_market_trades}")
    print(f"trades_read_total={stats['trade_rows']}")
    print(f"trade_requests={stats['trade_requests']}")
    print(f"positions_requests={stats['position_requests']}")
    print(f"trade_offset_cap_reached={'yes' if stats['hit_offset_cap'] else 'no'}")
    print(f"positions_offset_cap_reached={'yes' if stats['hit_position_cap'] else 'no'}")
    if raw_examples:
        print("raw_examples=" + json.dumps(raw_examples, ensure_ascii=False, separators=(",", ":")))
    print("tolkovanie")
    print("Скрипт сравнивает знакованную сумму сделок с размером каждой открытой позиции; расхождение показывает долю позиции, не объяснённую доступной историей сделок. Отдельный счётчик позиций без единой сделки по рынку — прямой индикатор получения долей вне биржевых сделок.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
