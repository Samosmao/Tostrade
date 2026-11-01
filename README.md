# XAU Signal

A full-stack **preview/MVP** for an XAU/USD signal dashboard. It has Flask accounts, a responsive candlestick chart, EMA/RSI/ATR indicators, and an experimental rule-based 1-minute setup. The public preview is **synthetic data**, not the gold market. A Twelve Data adapter is included but has not been verified against a real XAU/USD subscription; it needs your own authorized key and entitlement.

**This is not a trading service ready for public release, financial advice, a promise of profit, or a live trade-execution system.** Paper-test and backtest before using any signal. No stop-loss, take-profit, alerts, broker integration, or order execution is included.

## Preview locally

Requirements: Python 3.10+ and pip. From this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export APP_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
python app.py
```

Open `http://localhost:3000`. Create an account to try the member experience. Without `DATABASE_URL`, development accounts are stored in `data/app.sqlite3`; passwords are hashed with scrypt. No default account is provided. Without `APP_SECRET`, a temporary secret is generated and sessions reset on restart.

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:APP_SECRET = python -c "import secrets; print(secrets.token_hex(32))"
python app.py
```

The public market preview is always simulated. With no market-data key, signed-in members also see explicitly labeled simulated data. If a configured provider fails, the app shows an error rather than substituting demo prices.

## Experimental signal rules

The default **1m** view evaluates the latest **closed** 1-minute candle:

1. **5m bias:** the last complete 5-minute candle, constructed from five closed, contiguous 1-minute candles, must have EMA 9 above EMA 21 for a BUY or below it for a SELL. At least 21 consecutive complete 5-minute candles are required.
2. **1m trigger:** 1m EMA 9 must be above EMA 21 for BUY (below for SELL). The prior 1m close must be at or below its EMA 9 and the latest close above its EMA 9 for BUY; the comparison reverses for SELL.
3. **RSI confirmation:** 1m RSI 14 must be 52–68 inclusive for BUY or 32–48 inclusive for SELL. Otherwise the result is `WAIT`.
4. A missing recent bar, insufficient continuous history, or stale provider data forces `WAIT`. The dashboard reconstructs any BUY/SELL matches on the last five closed 1m bars, each using only candles available at that time. These are historical rule matches, not notifications or trade fills.

This can make **at most five closed-candle decisions in five minutes**, not the requested 10–20 entry signals. Rules can produce zero, and 120-second caching can delay or miss a timely trigger. A more frequent target would need a different event/tick-based strategy and a reliable feed that permits the intended use; neither the frequency nor profitability can be guaranteed.

On **5m, 15m, 1h and 4h**, the dashboard instead uses a simpler example: `BUY` if EMA 9 > EMA 21 and RSI 14 is 55–70; `SELL` if EMA 9 < EMA 21 and RSI is 30–45; otherwise `WAIT`. EMAs are initialized from the first close. RSI/ATR use Wilder smoothing, and ATR is informational only. These rules have not been optimized or backtested.

## Connecting Twelve Data

1. Verify with [Twelve Data](https://twelvedata.com/) that your specific account can request XAU/USD 1m `/time_series`, has acceptable latency, and permits display, attribution and any signal/derived-product use for your intended registered users. Provider licensing, including third-party redistribution, is a separate requirement from technical access; Basic/personal access may not permit a public or commercial signal service. See [commercial versus personal use](https://support.twelvedata.com/en/articles/5332349-commercial-and-personal-usage) and [attribution guidelines](https://support.twelvedata.com/en/articles/12647398-attribution-guidelines-for-using-twelve-data); add any market-specific attribution required by your contract.
2. Set **one** `TWELVE_DATA_API_KEY` as a secret server environment variable. Sign in to request provider data. Never put a key in browser code, a repository, or chat.
3. The server polls `/time_series` and caches per interval and process for `MARKET_CACHE_SECONDS` (default **120**, minimum 30). The browser asks the server once per minute or on refresh; those requests can be served from the cache. This is **not streaming**, and a provider may itself supply delayed data. The latest forming bar, when provided, is visibly marked; signals and displayed indicator values use only closed bars.
4. For the 1m strategy, if the last closed candle is at least 60 seconds old, the app forces `WAIT`. Longer timeframes use a threshold of three bars or 30 minutes, whichever is longer. These freshness checks are not market-hours detectors. A forming bar cached past its close is discarded, never promoted to a closed bar without a fresh provider response.

The free allowance can be exhausted quickly: at a 120-second interval, one actively requested timeframe could use about 720 credits/day before any other requests; multiple intervals or workers add more. Provider pricing and quotas can change. The UI shows the last poll and cache duration. Provider errors, including quota exhaustion, are shown without exposing the key; no fake fallback is used. Errors are cached for up to 60 seconds per interval to avoid repeated retries.

**Five-key fallback is intentionally disabled.** Separate free accounts do not establish permission to pool quotas or bypass limits; check the provider's [terms](https://twelvedata.com/terms) and obtain explicit authorization before implementing a compliant fallback. If uninterrupted, timely 1m signals and third-party display are needed, select a feed and plan that expressly support the latency, usage, licensing and frequency.

## Deploy on Render Free with Supabase Free

These instructions prepare a deployment; no Render or Supabase account is connected or deployed from this workspace. Free-tier terms and limits may change.

1. Create a GitHub/GitLab repository containing this project's files, including `render.yaml`. Do **not** commit `data/`, `.env`, keys, database credentials, or session secrets. The included `.gitignore` excludes the local database and common secret files.
2. Create a Supabase project. In its **Connect** panel, copy the PostgreSQL **session pooler** URI suitable for an IPv4-only persistent backend. Insert the database password as instructed by Supabase; percent-encode special characters in the password if necessary. Use SSL (`sslmode=require`) and keep this URI private. See [Supabase connection options](https://supabase.com/docs/guides/database/connecting-to-postgres). The application connects directly to PostgreSQL; its accounts are **not** Supabase Auth accounts.
3. In Render, create a **Blueprint** from that repository and review the proposed free web service. When prompted for `DATABASE_URL`, provide the Supabase session-pooler URI. `render.yaml` configures Gunicorn, a generated `APP_SECRET`, secure cookies, one worker, and a 120-second cache.
4. Deploy and wait for `/api/health` to become healthy. Open the Render URL and register a test account. Restart/redeploy the service and sign back in to verify that the account persists in Supabase. Do not assume persistence until you have tested it on your actual project.
5. When you have a suitable licensed feed, add `TWELVE_DATA_API_KEY` under the Render service's secret environment variables and redeploy/restart. Sign in, check the source/poll timestamp, and verify 1m candles and entitlement. The public preview still shows simulated data.

On startup the application creates `xau_private.users`, revokes public schema/table privileges, and enables default-deny row-level security. The server connects with the database role that owns the table and can bypass RLS. Do not expose the `xau_private` schema in Supabase's Data API, and do not use a public/anon key as `DATABASE_URL`. Review database permissions and backups before launch.

Render's free web service can sleep after inactivity and has an ephemeral local filesystem; it is not a continuously running signal broadcaster. Never rely on local SQLite on Render. The external database is necessary for persistent accounts. Supabase free projects can also pause after inactivity, and both platforms have quotas and changing terms: see [Render Free](https://render.com/docs/free) and [Supabase pricing](https://supabase.com/pricing).

## Verification and production work

```bash
python -W error::ResourceWarning -m unittest discover -s tests -v
```

The tests cover accounts/CSRF, mock provider and quota errors, closed/forming separation, cache staleness, 1m aggregation, BUY/SELL boundaries, gaps and historical reconstruction without lookahead. PostgreSQL registration/login was separately tested against a local PostgreSQL instance, **not** against Supabase. Real Twelve Data, Render deployment and Supabase connectivity remain unverified.

Before publishing to others: add verified email and password-reset flows, robust shared rate limiting and abuse protection, observability and error handling, database backups, privacy/terms documentation, a security review and appropriate financial/regulatory review. The built-in throttle and cache are per process and are not sufficient for a multi-worker production service. Live-feed permission, latency and continuity must be independently verified, and all strategies need out-of-sample backtesting and paper testing. Free sleeping hosting and 120-second polling are unsuitable for dependable real-time scalping.

### Project layout

- `app.py` — Flask routes, account sessions, CSRF, single-key provider cache.
- `storage.py` — local SQLite and PostgreSQL account storage.
- `market.py` — simulated/provider OHLC, indicators, signal rules.
- `render.yaml` — Render Free Blueprint.
- `static/` — dashboard, chart, icons and locally hosted fonts.
- `tests/` — automated tests.

DM Sans, DM Mono and DM Serif Display are distributed under the SIL Open Font License; license texts are included in `static/fonts/`.
