# rsibot

Scans large-cap, liquid EVM coins on [CoinGecko](https://www.coingecko.com/) and flags
the ones with RSI **under 30 (oversold)** or **over 70 (overbought)**.

## How it works

1. **Finds EVM coins.** It pulls CoinGecko's chain list and treats every chain with an
   EVM chain ID (Ethereum, BNB Chain, Base, Arbitrum, Polygon, Avalanche, ~270 more) as
   EVM. A coin counts if it's the gas coin of one of those chains (ETH, BNB, AVAX, HYPE,
   ...) or a token whose **home chain** is one of them. Non-EVM coins with bridged copies
   on EVM chains (ICP, TON, ...) don't count. BTC, XRP, ADA and similar are excluded even
   though some EVM sidechains use them as gas (see `NON_EVM_COINS`).
2. **Filters for size and liquidity.** From the top 2000 coins by market cap, it keeps
   those with market cap ≥ $25M and 24h volume ≥ $1M (volume stands in for liquidity).
   That's about 300 coins. Stablecoins, tokenized gold and stocks, and wrapped,
   liquid-staking and bridged tokens are dropped because they either don't move or copy
   another asset's price.
3. **Computes RSI.** It pulls price history for each coin and computes Wilder's RSI(14)
   on daily, 4h or 1h candles. Coins with under ~6 weeks of history or broken chart data
   are skipped, since their RSI isn't meaningful.

## Setup

```bash
pip install -r requirements.txt
export COINGECKO_API_KEY=your-key
```

Any CoinGecko key works. The scanner detects whether it's a free Demo key or a paid plan
(Lite, Pro, ...) and uses the right endpoint and pace. It also runs without a key, but
the keyless rate limit is very low.

## Usage

```bash
# Default: daily RSI(14), every EVM coin with mcap >= $25M and volume >= $1M
python rsi_scanner.py

# Bigger coins only, 4h candles, save to CSV
python rsi_scanner.py --timeframe 4h --min-mcap 1e9 --min-volume 20e6 --csv signals.csv

# Even more coins (~550): mcap >= $10M, volume >= $500K
python rsi_scanner.py --min-mcap 10e6 --min-volume 500e3

# Show every coin scanned, not just the signals
python rsi_scanner.py --all

# Custom thresholds
python rsi_scanner.py --oversold 25 --overbought 75
```

| Option | Default | Meaning |
|---|---|---|
| `--min-mcap` | `25e6` | Minimum market cap (USD) |
| `--min-volume` | `1e6` | Minimum 24h volume (USD) |
| `--max-coins` | `0` (no cap) | Max coins to compute RSI for, largest first |
| `--pages` | `8` | Pages of 250 coins to pull by market cap (8 = top 2000) |
| `--timeframe` | `1d` | `1d`, `4h` or `1h` candles |
| `--period` | `14` | RSI period |
| `--oversold` / `--overbought` | `30` / `70` | Signal thresholds |
| `--include-stables-and-wrapped` | off | Keep stablecoins, tokenized gold/stocks and wrapped/staked/bridged tokens |
| `--all` | off | Print every scanned coin |
| `--csv PATH` | – | Also write results to CSV |
| `--api-key` | `$COINGECKO_API_KEY` | CoinGecko key |
| `--pro` | auto | Force the paid-plan API (normally detected from the key) |
| `--workers` | `8` | Parallel requests (paid keys only) |
| `--delay` | auto | Seconds between API calls |
| `--email` | off | Email the signals (see below) |
| `--email-always` | off | With `--email`, also send on days with no signals |

How many coins each filter level scans (October 2026):

| `--min-mcap` | `--min-volume` | Coins |
|---|---|---|
| `100e6` | `5e6` | ~105 |
| `50e6` | `2e6` | ~195 |
| `25e6` | `1e6` | ~310 (default) |
| `10e6` | `500e3` | ~550 |

## Daily email alerts (GitHub Actions)

`.github/workflows/daily-scan.yml` scans every coin that passes the filters every day at 00:17 UTC, just
after the daily candle closes. It emails you only when at least one coin is past 30 or
70. Each run's CSV is saved as a workflow artifact.

1. **Gmail app password.** In your Google account, turn on 2-Step Verification, then
   create an app password at https://myaccount.google.com/apppasswords. Your normal
   Gmail password won't work.
2. **Repo secrets.** Go to GitHub → repo **Settings → Secrets and variables → Actions →
   New repository secret** and add:

   | Secret | Value |
   |---|---|
   | `COINGECKO_API_KEY` | Your CoinGecko key (Demo or paid) |
   | `SMTP_USER` | Your Gmail address |
   | `SMTP_PASSWORD` | The 16-character app password |

3. **Test it.** Go to **Actions → Daily RSI scan → Run workflow**.

**Can't get a Gmail app password?** (Common with work/Google Workspace accounts.) Use a
free [Brevo](https://www.brevo.com/) account instead (300 emails/day free):

1. Sign up, then add and verify your sender address under **Senders, Domains & Dedicated IPs → Senders**.
2. Under **SMTP & API → SMTP**, generate an SMTP key.
3. Set these repository secrets instead of the Gmail ones:

   | Secret | Value |
   |---|---|
   | `SMTP_HOST` | `smtp-relay.brevo.com` |
   | `SMTP_PORT` | `587` |
   | `SMTP_USER` | The SMTP **login** shown on that page (looks like `xxxx@smtp-brevo.com`) |
   | `SMTP_PASSWORD` | The SMTP key |
   | `EMAIL_FROM` | Your verified sender address |

Alerts go to `aj.ryder@outlook.com`. To change that, edit `EMAIL_TO` in the workflow
(comma-separate for several addresses) or `DEFAULT_EMAIL_TO` in `rsi_scanner.py`.

To get an email every day even when nothing triggers, add `--email-always` to the
workflow's `run` line. For a provider other than Gmail, also set `SMTP_HOST` and
`SMTP_PORT` (STARTTLS, default `smtp.gmail.com:587`) and optionally `EMAIL_FROM`.

The same flags work locally:

```bash
export SMTP_USER=you@gmail.com SMTP_PASSWORD=your-app-password
python rsi_scanner.py --email
```

## Speed and API limits

Each coin's chart is one request, plus about 25 setup calls per scan.

| Plan | Rate limit | Monthly calls | Default scan (~310 coins) |
|---|---|---|---|
| No key | very low, varies | – | 30+ min |
| Free Demo | 100/min | 10,000 | ~4 min |
| Paid (Lite and up) | 500/min | 2,000,000 | **~50 s** (8 parallel requests) |

With a **paid key** the monthly credits are effectively unlimited for this: a default
daily scan uses about 10,000 credits a month, 0.5% of Lite's 2M. Rate limit was measured
at 500 calls per rolling minute; the scanner paces itself at ~450/min.

With a **free Demo key**, a daily default scan uses about 10,500 calls a month, just
over the 10,000 cap. Add `--max-coins 300` or raise `--min-mcap` to stay under it.

*Not financial advice. RSI is one momentum signal and can stay extreme for a long time
in a strong trend.*
