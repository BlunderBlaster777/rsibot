# rsibot

Scans large-cap, liquid EVM coins on [CoinGecko](https://www.coingecko.com/) and flags
the ones with RSI **under 30 (oversold)** or **over 70 (overbought)**.

## How it works

1. **Finds EVM coins.** It pulls CoinGecko's chain list and treats every chain with an
   EVM chain ID (Ethereum, BNB Chain, Base, Arbitrum, Polygon, Avalanche, ~270 more) as
   EVM. A coin counts if it's the gas coin of one of those chains (ETH, BNB, AVAX, ...)
   or has a contract address on one. BTC, XRP, ADA and similar coins are excluded even
   though some EVM sidechains use them as gas (see `NON_EVM_GAS_COINS`).
2. **Filters for size and liquidity.** It keeps coins from the top 1000 by market cap with
   market cap ≥ $100M and 24h volume ≥ $5M (volume stands in for liquidity).
   Stablecoins and wrapped, liquid-staking and bridged tokens are dropped because they
   either don't move or copy another coin's price.
3. **Computes RSI.** It pulls price history for each coin and computes Wilder's RSI(14)
   on daily, 4h or 1h candles.

## Setup

```bash
pip install -r requirements.txt
```

**Get a free CoinGecko Demo API key**: https://www.coingecko.com/en/api/pricing.
The scanner works without a key, but the keyless rate limit is very low, so a scan
takes about 3x longer.

```bash
export COINGECKO_API_KEY=your-demo-key
```

## Usage

```bash
# Default: daily RSI(14), up to 150 coins, mcap >= $100M, volume >= $5M
python rsi_scanner.py

# 4h candles, bigger coins only, save to CSV
python rsi_scanner.py --timeframe 4h --min-mcap 1e9 --min-volume 20e6 --csv signals.csv

# Show every coin scanned, not just the signals
python rsi_scanner.py --all

# Custom thresholds
python rsi_scanner.py --oversold 25 --overbought 75
```

| Option | Default | Meaning |
|---|---|---|
| `--min-mcap` | `100e6` | Minimum market cap (USD) |
| `--min-volume` | `5e6` | Minimum 24h volume (USD) |
| `--max-coins` | `150` | How many coins to compute RSI for, largest first |
| `--pages` | `4` | Pages of 250 coins to pull by market cap (4 = top 1000) |
| `--timeframe` | `1d` | `1d`, `4h` or `1h` candles |
| `--period` | `14` | RSI period |
| `--oversold` / `--overbought` | `30` / `70` | Signal thresholds |
| `--include-stables-and-wrapped` | off | Keep stablecoins and wrapped/staked/bridged tokens |
| `--all` | off | Print every scanned coin |
| `--csv PATH` | – | Also write results to CSV |
| `--api-key` | `$COINGECKO_API_KEY` | CoinGecko key |
| `--pro` | off | The key is a paid Pro key |
| `--delay` | auto | Seconds between API calls |

## Speed

CoinGecko charts one coin per request, so scan time is set by the rate limit.
With the defaults (about 150 coins plus about 10 setup calls):

| Plan | Pace | Default scan |
|---|---|---|
| No key | 1 call / 6s | ~16 min |
| Free Demo key | ~27 calls/min | ~6 min |
| Pro key (`--pro`) | 2 calls/s | ~1.5 min |

Use `--max-coins` or a higher `--min-mcap` to scan fewer coins.

*Not financial advice. RSI is one momentum signal and can stay extreme for a long time
in a strong trend.*
