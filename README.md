# rsibot

Scans large-cap, liquid EVM coins on [CoinGecko](https://www.coingecko.com/) and flags
the ones with RSI **under 30 (oversold)** or **over 70 (overbought)**.

## How it works

1. **Finds EVM coins.** It pulls CoinGecko's chain list and treats every chain with an
   EVM chain ID (Ethereum, BNB Chain, Base, Arbitrum, Polygon, Avalanche, ~270 more) as
   EVM. A coin counts if it's the gas coin of one of those chains (ETH, BNB, AVAX, ...)
   or has a contract address on one. BTC, XRP, ADA and similar coins are excluded even
   though some EVM sidechains use them as gas or host bridged copies (see `NON_EVM_COINS`).
2. **Filters for size and liquidity.** It keeps coins from the top 1000 by market cap with
   market cap ≥ $100M and 24h volume ≥ $5M (volume stands in for liquidity).
   Stablecoins, tokenized gold, and wrapped, liquid-staking and bridged tokens are dropped because they
   either don't move or copy another asset's price.
3. **Computes RSI.** It pulls price history for each coin and computes Wilder's RSI(14)
   on daily, 4h or 1h candles.

## Setup

```bash
pip install -r requirements.txt
```

**Get a free CoinGecko Demo API key**: https://www.coingecko.com/en/api/pricing
(100 calls/min, 10,000 calls/month).
The scanner works without a key, but the keyless rate limit is very low, so a scan
takes several times longer.

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
| `--include-stables-and-wrapped` | off | Keep stablecoins, tokenized gold and wrapped/staked/bridged tokens |
| `--all` | off | Print every scanned coin |
| `--csv PATH` | – | Also write results to CSV |
| `--api-key` | `$COINGECKO_API_KEY` | CoinGecko key |
| `--pro` | off | The key is a paid Pro key |
| `--delay` | auto | Seconds between API calls |
| `--email` | off | Email the signals (see below) |
| `--email-always` | off | With `--email`, also send on days with no signals |

## Daily email alerts (GitHub Actions)

`.github/workflows/daily-scan.yml` scans up to 300 coins every day at 00:17 UTC, just
after the daily candle closes. It emails you only when at least one coin is past 30 or
70. Each run's CSV is saved as a workflow artifact.

1. **Gmail app password.** In your Google account, turn on 2-Step Verification, then
   create an app password at https://myaccount.google.com/apppasswords. Your normal
   Gmail password won't work.
2. **Repo secrets.** Go to GitHub → repo **Settings → Secrets and variables → Actions →
   New repository secret** and add:

   | Secret | Value |
   |---|---|
   | `COINGECKO_API_KEY` | Your CoinGecko Demo key |
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
python rsi_scanner.py --max-coins 300 --email
```

## Speed

CoinGecko charts one coin per request, so scan time is set by the rate limit.
Each scan also spends about 12 calls on setup.

| Plan | Pace | 150 coins (default) | 300 coins |
|---|---|---|---|
| No key | 1 call / 6s, often throttled | 20–40 min | too slow |
| Free Demo key | ~85 calls/min | ~2 min | ~4 min |
| Pro key (`--pro`) | 5 calls/s | <1 min | ~1 min |

The Demo key's **10,000 calls/month** is the real cap for a daily job. One scan a day of
about 300 coins (about 9,700 calls/month) is the most that fits. To scan more coins,
or more than once a day, you need a paid plan. Otherwise use a higher `--min-mcap` to
spend calls on bigger coins only.

*Not financial advice. RSI is one momentum signal and can stay extreme for a long time
in a strong trend.*
