#!/usr/bin/env python3
"""Scan large-cap, liquid EVM coins on CoinGecko for oversold / overbought RSI.

A coin counts as "EVM" if it is the native coin of an EVM chain (ETH, BNB,
AVAX, ...) or a token whose home chain is EVM. EVM chains are taken
from CoinGecko's /asset_platforms: every platform with a numeric
chain_identifier (the EVM chain ID).
"""

import argparse
import csv
import html
import os
import smtplib
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.message import EmailMessage

import requests

FREE_BASE = "https://api.coingecko.com/api/v3"
PRO_BASE = "https://pro-api.coingecko.com/api/v3"

# Categories to drop by default: they either don't move (stablecoins) or just
# mirror another asset's price (wrapped / staked / bridged versions, gold,
# tokenized stocks).
DEFAULT_EXCLUDE_CATEGORIES = [
    "stablecoins",
    "wrapped-tokens",
    "liquid-staking-tokens",
    "bridged-tokens",
    "tokenized-gold",
    "tokenized-stock",
]

# Coins whose home chain is not EVM but that some EVM chain uses as its gas
# token (Bitcoin L2s, XRPL EVM, Milkomeda, Etherlink, ...).
NON_EVM_COINS = {
    "bitcoin", "ripple", "cardano", "tezos", "the-open-network", "bitcoin-cash",
    "eos", "flow", "iota", "zilliqa", "nuls", "defichain", "bittensor", "near",
}

DEFAULT_EMAIL_TO = "aj.ryder@outlook.com"

# Timeframe -> (endpoint, params). CoinGecko picks candle size from `days`:
#   market_chart: 2-90 days -> hourly points, >90 days -> daily points
#   ohlc:         3-30 days -> 4h candles
TIMEFRAMES = {
    "1h": ("market_chart", {"days": "7"}),
    "4h": ("ohlc", {"days": "30"}),
    "1d": ("market_chart", {"days": "120", "interval": "daily"}),
}


class CoinGecko:
    def __init__(self, api_key=None, pro=None, delay=None):
        self.session = requests.Session()
        self.api_key = api_key
        if api_key and pro is None:
            pro = self._is_pro_key(api_key)
        self.pro = bool(pro)
        self.base = PRO_BASE if self.pro else FREE_BASE
        if api_key:
            header = "x-cg-pro-api-key" if self.pro else "x-cg-demo-api-key"
            self.session.headers[header] = api_key
        # Paid plans: 500+ calls/min (we aim for ~450). Free demo key: 100/min.
        # No key: the shared public limit, which is much lower and varies.
        if delay is None:
            delay = 0.135 if self.pro else 0.7 if api_key else 6.0
        self.delay = delay
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def _is_pro_key(self, api_key):
        """Paid (Pro/Lite/...) keys only work on pro-api; demo keys only on api."""
        for attempt in range(4):
            try:
                resp = requests.get(PRO_BASE + "/ping", timeout=15,
                                    headers={"x-cg-pro-api-key": api_key})
                if resp.status_code == 200:
                    return True
                if resp.status_code in (400, 401, 403):
                    break
            except requests.RequestException:
                pass
            time.sleep(5 * (attempt + 1))  # rate limited or network trouble
        # The free endpoint rejects paid keys with an "use pro-api" error.
        try:
            resp = requests.get(FREE_BASE + "/ping", timeout=15,
                                headers={"x-cg-demo-api-key": api_key})
            return resp.status_code == 400 and "pro-api" in resp.text
        except requests.RequestException:
            return False

    def _wait_turn(self):
        # Hand out evenly spaced call slots so parallel workers share one rate limit.
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.delay
        if slot > now:
            time.sleep(slot - now)

    def get(self, path, **params):
        for attempt in range(6):
            self._wait_turn()
            try:
                resp = self.session.get(self.base + path, params=params, timeout=30)
            except requests.RequestException as exc:
                backoff = 2 ** attempt
                print(f"  network error ({exc}); retrying in {backoff}s", file=sys.stderr)
                time.sleep(backoff)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                backoff = int(resp.headers.get("Retry-After", 0)) or (
                    10 if self.pro else 30) * (attempt + 1)
                print(f"  HTTP {resp.status_code} on {path}; waiting {backoff}s", file=sys.stderr)
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"giving up on {path} after repeated failures")

    def usage(self):
        """Monthly credit usage for paid keys, or None."""
        if not self.pro:
            return None
        try:
            return self.get("/key")
        except (requests.HTTPError, RuntimeError):
            return None


def evm_platforms(cg):
    """Return (EVM platform ids, native coin ids of EVM chains)."""
    platforms = cg.get("/asset_platforms")
    evm = [p for p in platforms if p.get("chain_identifier") is not None]
    native = {p["native_coin_id"] for p in evm if p.get("native_coin_id")}
    return {p["id"] for p in evm}, native


def evm_coin_ids(cg):
    platform_ids, native_ids = evm_platforms(cg)
    coins = cg.get("/coins/list", include_platform="true")
    ids = set(native_ids)
    for coin in coins:
        # The first platform listed is the token's home chain. Later ones are
        # bridged copies, which non-EVM coins (ICP, TON, ...) often have too.
        home = next(iter((coin.get("platforms") or {}).items()), None)
        if home and home[0] in platform_ids and home[1]:
            ids.add(coin["id"])
    return ids - NON_EVM_COINS


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


def bad_history(values, current_price, period):
    """Reason to distrust a price history for RSI, or None if it looks fine."""
    # Wilder's smoothing needs a few periods of history to settle.
    if len(values) < 3 * period + 1:
        return f"only {len(values)} candles of history"
    if any(v is None or v <= 0 for v in values[-(3 * period + 1):]):
        return "zero or missing prices in the chart"
    # The chart's last point is the live price; if it disagrees with the
    # market data, the chart is stale or broken.
    if current_price and abs(values[-1] / current_price - 1) > 0.5:
        return f"chart price {values[-1]:.6g} doesn't match market price {current_price:.6g}"
    return None


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


def build_email(results, args):
    """Return (subject, plain text, html) for the scan results."""
    signals = sorted((r for r in results if r["signal"]), key=lambda r: r["rsi"])
    oversold = [r for r in signals if r["signal"] == "OVERSOLD"]
    overbought = [r for r in reversed(signals) if r["signal"] == "OVERBOUGHT"]
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    subject = (f"RSI scan {date}: {len(oversold)} oversold, {len(overbought)} overbought"
               if signals else f"RSI scan {date}: no signals")

    def text_rows(rows):
        return "\n".join(f"  {r['symbol']:<8} {r['name'][:24]:<25} RSI {r['rsi']:5.1f}  "
                         f"${r['price']:.6g}  mcap {fmt_usd(r['market_cap'])}" for r in rows)

    def html_table(title, rows, color):
        if not rows:
            return ""
        cells = "".join(
            f"<tr><td><b>{html.escape(r['symbol'])}</b></td><td>{html.escape(r['name'])}</td>"
            f"<td align='right' style='color:{color}'><b>{r['rsi']:.1f}</b></td>"
            f"<td align='right'>${r['price']:.6g}</td>"
            f"<td align='right'>{fmt_usd(r['market_cap'])}</td>"
            f"<td align='right'>{fmt_usd(r['volume_24h'])}</td>"
            f"<td><a href='https://www.coingecko.com/en/coins/{r['id']}'>chart</a></td></tr>"
            for r in rows)
        return (f"<h3 style='color:{color}'>{title}</h3>"
                "<table cellpadding='6' style='border-collapse:collapse;font-family:sans-serif'>"
                "<tr style='background:#f0f0f0'><th align='left'>Symbol</th><th align='left'>Name</th>"
                "<th>RSI</th><th>Price</th><th>Mcap</th><th>Vol 24h</th><th></th></tr>"
                f"{cells}</table>")

    settings = (f"{args.timeframe} RSI({args.period}), {len(results)} EVM coins scanned, "
                f"mcap >= {fmt_usd(args.min_mcap)}, 24h volume >= {fmt_usd(args.min_volume)}")
    text = [subject, settings, ""]
    if oversold:
        text += [f"OVERSOLD (RSI < {args.oversold:g})", text_rows(oversold), ""]
    if overbought:
        text += [f"OVERBOUGHT (RSI > {args.overbought:g})", text_rows(overbought), ""]
    if not signals:
        text.append("No coins past the RSI thresholds today.")
    body_html = (f"<p style='font-family:sans-serif;color:#555'>{html.escape(settings)}</p>"
                 + html_table(f"Oversold (RSI &lt; {args.oversold:g})", oversold, "#1a7f37")
                 + html_table(f"Overbought (RSI &gt; {args.overbought:g})", overbought, "#cf222e")
                 + ("" if signals else "<p>No coins past the RSI thresholds today.</p>"))
    return subject, "\n".join(text), f"<html><body>{body_html}</body></html>"


def send_email(results, args):
    """Email the signals via SMTP. Settings come from environment variables."""
    signals = [r for r in results if r["signal"]]
    if not signals and not args.email_always:
        print("No signals; skipping email.", file=sys.stderr)
        return

    env = os.environ
    missing = [k for k in ("SMTP_USER", "SMTP_PASSWORD") if not env.get(k)]
    if missing:
        sys.exit(f"--email needs these environment variables: {', '.join(missing)}")
    to = env.get("EMAIL_TO") or DEFAULT_EMAIL_TO

    subject, text, body_html = build_email(results, args)
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = env.get("EMAIL_FROM") or env["SMTP_USER"]
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(body_html, subtype="html")

    host = env.get("SMTP_HOST") or "smtp.gmail.com"
    port = int(env.get("SMTP_PORT") or 587)
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.starttls()
        smtp.login(env["SMTP_USER"], env["SMTP_PASSWORD"])
        smtp.send_message(msg)
    print(f"Emailed {len(signals)} signals to {to}", file=sys.stderr)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--min-mcap", type=float, default=25e6,
                   help="minimum market cap in USD (default 25M)")
    p.add_argument("--min-volume", type=float, default=1e6,
                   help="minimum 24h volume in USD, used as the liquidity filter (default 1M)")
    p.add_argument("--max-coins", type=int, default=0,
                   help="max coins to compute RSI for, largest first (default 0 = no cap)")
    p.add_argument("--pages", type=int, default=8,
                   help="pages of 250 coins to pull by market cap (default 8 = top 2000)")
    p.add_argument("--timeframe", choices=TIMEFRAMES, default="1d",
                   help="candle timeframe for RSI (default 1d)")
    p.add_argument("--period", type=int, default=14, help="RSI period (default 14)")
    p.add_argument("--oversold", type=float, default=30, help="oversold threshold (default 30)")
    p.add_argument("--overbought", type=float, default=70, help="overbought threshold (default 70)")
    p.add_argument("--include-stables-and-wrapped", action="store_true",
                   help="don't drop stablecoins, tokenized gold and wrapped/staked/bridged tokens")
    p.add_argument("--all", action="store_true",
                   help="print every scanned coin, not just ones past the thresholds")
    p.add_argument("--csv", metavar="PATH", help="also write results to a CSV file")
    p.add_argument("--api-key", default=os.environ.get("COINGECKO_API_KEY"),
                   help="CoinGecko API key (or set COINGECKO_API_KEY)")
    p.add_argument("--pro", action="store_true", default=None,
                   help="force the paid-plan API (normally detected from the key)")
    p.add_argument("--delay", type=float, help="seconds between API calls")
    p.add_argument("--workers", type=int, default=8,
                   help="parallel requests for paid keys (default 8)")
    p.add_argument("--email", action="store_true",
                   help="email the signals (SMTP settings from environment variables)")
    p.add_argument("--email-always", action="store_true",
                   help="with --email, send even when there are no signals")
    return p.parse_args()


def main():
    args = parse_args()
    cg = CoinGecko(args.api_key, args.pro, args.delay)
    plan = "paid plan" if cg.pro else "demo key" if args.api_key else "no API key"
    print(f"Using CoinGecko {plan}.", file=sys.stderr)

    print("Loading EVM chains and coin list...", file=sys.stderr)
    evm_ids = evm_coin_ids(cg)

    excluded = set()
    if not args.include_stables_and_wrapped:
        for cat in DEFAULT_EXCLUDE_CATEGORIES:
            print(f"Loading excluded category: {cat}", file=sys.stderr)
            excluded |= category_ids(cg, cat, pages=8)

    print(f"Loading top {args.pages * 250} coins by market cap...", file=sys.stderr)
    markets = top_markets(cg, args.pages)
    candidates = [
        m for m in markets
        if m["id"] in evm_ids
        and m["id"] not in excluded
        and (m.get("market_cap") or 0) >= args.min_mcap
        and (m.get("total_volume") or 0) >= args.min_volume
    ]
    if args.max_coins:
        candidates = candidates[: args.max_coins]
    print(f"{len(candidates)} EVM coins pass the filters; computing {args.timeframe} "
          f"RSI({args.period})...", file=sys.stderr)

    def coin_rsi(m):
        try:
            values = closes(cg, m["id"], args.timeframe)
        except (requests.HTTPError, RuntimeError) as exc:
            print(f"  {m['symbol'].upper()}: skipped ({exc})", file=sys.stderr)
            return None
        problem = bad_history(values, m.get("current_price"), args.period)
        if problem:
            print(f"  {m['symbol'].upper()}: skipped ({problem})", file=sys.stderr)
            return None
        return rsi(values, args.period)

    # Only paid keys have the headroom for parallel requests; the shared rate
    # limiter in CoinGecko.get keeps workers under the per-minute cap.
    workers = max(1, args.workers) if cg.pro else 1
    started = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        values = list(pool.map(coin_rsi, candidates))
    skipped = sum(v is None for v in values)
    print(f"Fetched {len(candidates)} charts in {time.monotonic() - started:.0f}s"
          f"{f'; skipped {skipped} with unusable data' if skipped else ''}.", file=sys.stderr)

    results = []
    for m, value in zip(candidates, values):
        if value is None:
            continue
        signal = ("OVERSOLD" if value < args.oversold
                  else "OVERBOUGHT" if value > args.overbought else "")
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

    if args.email:
        send_email(results, args)

    usage = cg.usage()
    if usage:
        print(f"API credits used this month: {usage.get('api_key_current_total_monthly_calls')}"
              f" of {usage.get('api_key_monthly_call_credit')}", file=sys.stderr)


if __name__ == "__main__":
    main()
