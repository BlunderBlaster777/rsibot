#!/usr/bin/env python3
"""Scan large-cap, liquid EVM coins on CoinGecko for oversold / overbought RSI.

A coin counts as "EVM" if it is the native coin of an EVM chain (ETH, BNB,
AVAX, ...) or has a contract address on any EVM chain. EVM chains are taken
from CoinGecko's /asset_platforms: every platform with a numeric
chain_identifier (the EVM chain ID).
"""

import argparse
import csv
import os
import sys
import time

import requests

FREE_BASE = "https://api.coingecko.com/api/v3"
PRO_BASE = "https://pro-api.coingecko.com/api/v3"

# Categories to drop by default: they either don't move (stablecoins) or just
# mirror another coin's price (wrapped / staked / bridged versions).
DEFAULT_EXCLUDE_CATEGORIES = [
    "stablecoins",
    "wrapped-tokens",
    "liquid-staking-tokens",
    "bridged-tokens",
]

# Coins that some EVM chain borrows as its gas token (Bitcoin L2s, XRPL EVM,
# Milkomeda, Etherlink, ...) but whose home chain is not EVM. Without this they'd
# be picked up as "native coins of an EVM chain".
NON_EVM_GAS_COINS = {
    "bitcoin", "ripple", "cardano", "tezos", "the-open-network", "bitcoin-cash",
    "eos", "flow", "iota", "zilliqa", "nuls", "defichain", "bittensor", "near",
}

# Timeframe -> (endpoint, params). CoinGecko picks candle size from `days`:
#   market_chart: 2-90 days -> hourly points, >90 days -> daily points
#   ohlc:         3-30 days -> 4h candles
TIMEFRAMES = {
    "1h": ("market_chart", {"days": "7"}),
    "4h": ("ohlc", {"days": "30"}),
    "1d": ("market_chart", {"days": "120", "interval": "daily"}),
}


class CoinGecko:
    def __init__(self, api_key=None, pro=False, delay=None):
        self.session = requests.Session()
        self.base = PRO_BASE if pro else FREE_BASE
        if api_key:
            header = "x-cg-pro-api-key" if pro else "x-cg-demo-api-key"
            self.session.headers[header] = api_key
        # Pro: 500+ calls/min. Free demo key: 30/min. No key: the shared public
        # limit, which is much lower and varies, so go slow.
        if delay is None:
            delay = 0.5 if pro else 2.2 if api_key else 6.0
        self.delay = delay
        self._last_call = 0.0

    def get(self, path, **params):
        for attempt in range(6):
            wait = self.delay - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self.session.get(self.base + path, params=params, timeout=30)
            except requests.RequestException as exc:
                backoff = 2 ** attempt
                print(f"  network error ({exc}); retrying in {backoff}s", file=sys.stderr)
                time.sleep(backoff)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                backoff = int(resp.headers.get("Retry-After", 0)) or 30 * (attempt + 1)
                print(f"  HTTP {resp.status_code} on {path}; waiting {backoff}s", file=sys.stderr)
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"giving up on {path} after repeated failures")


def evm_platforms(cg):
    """Return (EVM platform ids, native coin ids of EVM chains)."""
    platforms = cg.get("/asset_platforms")
    evm = [p for p in platforms if p.get("chain_identifier") is not None]
    native = {p["native_coin_id"] for p in evm if p.get("native_coin_id")}
    return {p["id"] for p in evm}, native - NON_EVM_GAS_COINS


def evm_coin_ids(cg):
    platform_ids, native_ids = evm_platforms(cg)
    coins = cg.get("/coins/list", include_platform="true")
    ids = set(native_ids)
    for coin in coins:
        if any(p in platform_ids and addr for p, addr in (coin.get("platforms") or {}).items()):
            ids.add(coin["id"])
    return ids


def category_ids(cg, category, pages):
    ids = set()
    for page in range(1, pages + 1):
        rows = cg.get("/coins/markets", vs_currency="usd", category=category,
                      order="market_cap_desc", per_page=250, page=page)
        ids.update(r["id"] for r in rows)
        if len(rows) < 250:
            break
    return ids


def top_markets(cg, pages):
    rows = []
    for page in range(1, pages + 1):
        batch = cg.get("/coins/markets", vs_currency="usd", order="market_cap_desc",
                       per_page=250, page=page)
        rows.extend(batch)
        if len(batch) < 250:
            break
    return rows


def closes(cg, coin_id, timeframe):
    endpoint, params = TIMEFRAMES[timeframe]
    data = cg.get(f"/coins/{coin_id}/{endpoint}", vs_currency="usd", **params)
    if endpoint == "ohlc":
        return [row[4] for row in data]
    return [price for _, price in data.get("prices", [])]


def rsi(values, period=14):
    """Wilder's RSI of the last value in `values`, or None if too short."""
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for prev, cur in zip(values, values[1:]):
        change = cur - prev
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for gain, loss in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
    if avg_loss == 0:
        return 100.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def fmt_usd(n):
    for unit, size in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= size:
            return f"${n / size:.2f}{unit}"
    return f"${n:,.2f}"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--min-mcap", type=float, default=100e6,
                   help="minimum market cap in USD (default 100M)")
    p.add_argument("--min-volume", type=float, default=5e6,
                   help="minimum 24h volume in USD, used as the liquidity filter (default 5M)")
    p.add_argument("--max-coins", type=int, default=150,
                   help="max coins to compute RSI for, largest first (default 150)")
    p.add_argument("--pages", type=int, default=4,
                   help="pages of 250 coins to pull by market cap (default 4 = top 1000)")
    p.add_argument("--timeframe", choices=TIMEFRAMES, default="1d",
                   help="candle timeframe for RSI (default 1d)")
    p.add_argument("--period", type=int, default=14, help="RSI period (default 14)")
    p.add_argument("--oversold", type=float, default=30, help="oversold threshold (default 30)")
    p.add_argument("--overbought", type=float, default=70, help="overbought threshold (default 70)")
    p.add_argument("--include-stables-and-wrapped", action="store_true",
                   help="don't drop stablecoins and wrapped/staked/bridged tokens")
    p.add_argument("--all", action="store_true",
                   help="print every scanned coin, not just ones past the thresholds")
    p.add_argument("--csv", metavar="PATH", help="also write results to a CSV file")
    p.add_argument("--api-key", default=os.environ.get("COINGECKO_API_KEY"),
                   help="CoinGecko API key (or set COINGECKO_API_KEY)")
    p.add_argument("--pro", action="store_true",
                   help="key is a paid Pro key (uses pro-api.coingecko.com)")
    p.add_argument("--delay", type=float, help="seconds between API calls")
    return p.parse_args()


def main():
    args = parse_args()
    cg = CoinGecko(args.api_key, args.pro, args.delay)

    print("Loading EVM chains and coin list...", file=sys.stderr)
    evm_ids = evm_coin_ids(cg)

    excluded = set()
    if not args.include_stables_and_wrapped:
        for cat in DEFAULT_EXCLUDE_CATEGORIES:
            print(f"Loading excluded category: {cat}", file=sys.stderr)
            excluded |= category_ids(cg, cat, pages=2)

    print(f"Loading top {args.pages * 250} coins by market cap...", file=sys.stderr)
    markets = top_markets(cg, args.pages)
    candidates = [
        m for m in markets
        if m["id"] in evm_ids
        and m["id"] not in excluded
        and (m.get("market_cap") or 0) >= args.min_mcap
        and (m.get("total_volume") or 0) >= args.min_volume
    ][: args.max_coins]
    print(f"{len(candidates)} EVM coins pass the filters; computing {args.timeframe} "
          f"RSI({args.period})...", file=sys.stderr)

    results = []
    for i, m in enumerate(candidates, 1):
        try:
            value = rsi(closes(cg, m["id"], args.timeframe), args.period)
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"  [{i}/{len(candidates)}] {m['symbol'].upper()}: skipped ({exc})", file=sys.stderr)
            continue
        if value is None:
            continue
        signal = ("OVERSOLD" if value < args.oversold
                  else "OVERBOUGHT" if value > args.overbought else "")
        print(f"  [{i}/{len(candidates)}] {m['symbol'].upper():<8} RSI {value:5.1f} {signal}",
              file=sys.stderr)
        results.append({
            "id": m["id"],
            "symbol": m["symbol"].upper(),
            "name": m["name"],
            "price": m["current_price"],
            "market_cap": m["market_cap"],
            "volume_24h": m["total_volume"],
            "change_24h_pct": m.get("price_change_percentage_24h"),
            "rsi": round(value, 2),
            "signal": signal,
        })

    shown = results if args.all else [r for r in results if r["signal"]]
    shown.sort(key=lambda r: r["rsi"])

    print()
    if not shown:
        print("No coins past the RSI thresholds right now.")
    else:
        print(f"{'SIGNAL':<11}{'SYMBOL':<9}{'NAME':<24}{'RSI':>6}{'PRICE':>14}"
              f"{'MCAP':>11}{'VOL 24H':>11}{'24H %':>8}")
        for r in shown:
            change = r["change_24h_pct"]
            print(f"{r['signal'] or '-':<11}{r['symbol']:<9}{r['name'][:23]:<24}"
                  f"{r['rsi']:>6.1f}{r['price']:>14.6g}{fmt_usd(r['market_cap']):>11}"
                  f"{fmt_usd(r['volume_24h']):>11}"
                  f"{(f'{change:.1f}' if change is not None else '-'):>8}")
    oversold = sum(r["signal"] == "OVERSOLD" for r in results)
    overbought = sum(r["signal"] == "OVERBOUGHT" for r in results)
    print(f"\nScanned {len(results)} coins: {oversold} oversold (<{args.oversold:g}), "
          f"{overbought} overbought (>{args.overbought:g}).")

    if args.csv:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0]) if results else ["id"])
            writer.writeheader()
            writer.writerows(shown)
        print(f"Wrote {len(shown)} rows to {args.csv}")


if __name__ == "__main__":
    main()
