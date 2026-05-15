# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Scan S&P 500 for breakout setups (dry run, prints table)
python scanner.py

# Scan specific symbols
python scanner.py --symbols AAPL NVDA AMD TSLA

# Scan and place Alpaca paper orders
python scanner.py --execute

# Bypass regime filter (useful for testing in bear/sideways markets)
python scanner.py --no-regime

# Simulate a trade day-by-day
python simulate.py --symbol AMD
python simulate.py --symbol MU --entry 2026-04-10

# Run the autonomous trading daemon (waits for market open, trades automatically)
python runner.py
python runner.py --dry-run          # no orders placed
python runner.py --interval 30      # rescan every 30 min

# Run the web dashboard
python -m webapp.app                # http://localhost:8000

# Run regression tests
python run_tests.py                 # run all 178 tests
python run_tests.py -k TestConfig   # run one test class
python run_tests.py -k "e2e"        # run E2E tests only
python run_tests.py -k "Security"   # run security tests only
python run_tests.py --cov           # with coverage report
python run_tests.py -v              # verbose output

# Backtest on ~50 symbols (1 year)
python scanner.py --backtest

# Grid-search parameter optimisation
python scanner.py --optimize
```

Install dependencies: `pip install -r requirements.txt`

Copy `.env.example` to `.env` and fill in Alpaca credentials before running anything that touches orders or positions.

## Git workflow

Commit and push to GitHub after every meaningful unit of work — a bug fix, a new feature, a config change, a refactor. Don't batch unrelated changes into one commit.

```bash
git add <specific files>
git commit -m "short description of what changed and why"
git push
```

Commit message style: imperative, lowercase, no period. Lead with the what, include the why when non-obvious. Examples:
- `reduce max risk per trade from 2% to 1%`
- `cap partial target at 1.5x entry to prevent unrealistic exits`
- `fix yfinance MultiIndex column crash on Windows`

Never commit `.env`, `*.db`, or `__pycache__/` — all excluded in `.gitignore`. The remote is `https://github.com/aftabk1/atrades` (private).

## Trading Strategy

### What the system trades
Momentum breakouts on S&P 500 equities. It looks for stocks breaking out of a consolidation base to new highs with institutional accumulation behind the move. It does not trade shorts, options, or intraday scalps — daily close data only.

### Entry conditions (all must pass)
A candidate must score ≥ 60/100 and pass the regime gate to qualify for a trade. The score is built from:

| Signal | Max pts | What it measures |
|---|---|---|
| Volume surge | 25 | Today's volume ≥ 1.5× 20-day average |
| 20-day breakout | 20 | Close above 20-day high (mandatory) |
| Relative strength | 15 | Outperforming SPY over last 20 days |
| RSI zone | 15 | RSI(14) between 55–70 (momentum, not overbought) |
| 50-day breakout | 15 | Close above 50-day high (multi-month conviction) |
| ATR expansion | 10 | Today's ATR > 1.2× 14-day average (volatility expanding) |
| Consolidation | 10 | Low daily volatility in prior 15 days (tight base) |
| Higher lows | 10 | Ascending lows over prior 15 days (demand building) |
| Earnings proximity | 5 | Catalyst within ±7 days |
| Accumulation bonus | +15 | OBV trend, CMF > 0, up/down volume ratio, block trade days |
| Bull trap penalty | −40 max | Reversal candles, low volume, gap-and-fade, resistance proximity |

### Market regime gate
The scanner checks SPY daily before scanning. In `HIGH_VOLATILITY` (realised vol > 30%) it suspends all scanning. In `BEAR_TREND` (SPY < 200MA) it applies a heavy score penalty. In `SIDEWAYS` it raises the score floor. Only `BULL_TREND` (SPY > 200MA, ADX ≥ 25, positive slope) runs at full sensitivity.

### Exit rules (3-order bracket per trade)
1. **Hard stop** — if price drops to stop before partial fills, exit all shares at stop. No order placed for this; must be monitored manually or via the runner.
2. **Partial exit (50% of shares)** — GTC limit sell at 2R target, capped at 1.5× entry.
3. **Trailing stop (remaining 50%)** — GTC trailing stop with trail distance = 2×ATR(14). After partial fills, the trail ratchets up each day: `MAX(trail_stop, close − 2×ATR, previous candle low)`.

### Risk limits
- Max loss per trade: 1% of portfolio ($1,000 on $100k)
- Max position size: 10% of portfolio
- Max concurrent open trades: 4
- Universe: 503 S&P 500 symbols by default (customisable via Config tab → Scanner Universe), min price $25, min avg volume 1M shares/day

## Architecture

### Signal → Score → Setup → Orders pipeline

`scanner.py` is the entry point for everything. One full scan pass:

1. **Universe** (`data/universe.py`) — returns the active symbol list. Checks `data/universe_override.json` first; falls back to the hardcoded 503 S&P 500 symbols.
2. **Market data** (`data/market_data.py`) — fetches OHLCV via yfinance.
3. **Regime gate** (`strategy/market_regime.py`) — classifies SPY into `BULL_TREND / SIDEWAYS / BEAR_TREND / HIGH_VOLATILITY` using ADX(14) and 200MA. Applies a `score_multiplier` to all candidates and raises the `min_score` floor in bad regimes.
4. **Signal detection** (`strategy/breakout_signals.py`) — `detect_all()` returns a `BreakoutSignals` dataclass per symbol with 9 factor signals. Symbols failing base filters (price, volume, missing data) return `None`.
5. **Accumulation** (`strategy/accumulation.py`) — OBV, CMF, up/down volume ratio, block days. Adds up to +15 pts.
6. **Bull trap detection** (`strategy/bull_trap.py`) — 5 warning signs; subtracts up to −40 pts from final score.
7. **Scorer** (`strategy/breakout_scorer.py`) — `BreakoutScorer.score()` produces a 0–100 confidence score.
8. **Trade setup** (`risk/trade_setup.py`) — `calculate_setup()` takes a `BreakoutSignals` + score and returns a `TradeSetup` with stop, target, sizing. Returns `None` when ATR=0 or risk-per-share is invalid.
9. **Order execution** (`execution/bracket_orders.py`) — 3 separate Alpaca orders: market buy (all shares), GTC limit sell (partial), GTC trailing stop sell (remainder).

`scanner.py` calls `save_scan()` after every run so scan results and candidates persist to the DB regardless of how the scanner is invoked (CLI or web dashboard Scan Now button).

### Stop / target / sizing rules

- **Stop** = `MAX(MIN(entry − 2×ATR14, SwingLow×0.995), entry×0.80)`
  - Takes the wider of ATR stop and 10-day swing low, floored at 80% of entry.
- **Partial target** = `MIN(entry + 2×risk_per_share, entry×1.50)`
  - 50% of shares exit at 2R, capped at 1.5× entry.
- **Trail** = remainder trailed with 2×ATR; ratcheted up using `MAX(trail_stop, close−trail_atr, prev_candle_low)`.
- **Position size** = `MIN(risk_budget / risk_per_share, position_cap / entry)` where `risk_budget = portfolio × 1%` and `position_cap = portfolio × 10%`.

Key config values live in `config.py` (all overridable via `.env`): `BREAKOUT_ATR_STOP_MULT=2.0`, `BREAKOUT_MAX_STOP_PCT=0.20`, `PARTIAL_EXIT_R=2.0`, `PARTIAL_EXIT_PCT=0.50`, `TRAIL_ATR_MULT=2.0`, `MAX_PORTFOLIO_RISK=0.01`, `MAX_POSITION_SIZE=0.10`, `BREAKOUT_MIN_SCORE=60.0`.

### Persistence and web dashboard

`data/store.py` — SQLite at `data/atrades.db`. Tables: `scan_runs`, `scan_candidates`, `trades`. `init_db()` is idempotent. `save_scan()` writes one run and all its candidates; `save_trade()` writes one placed order.

`webapp/app.py` — FastAPI server. Static files served from `webapp/static/`. API endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /login` | Login page (no auth required) |
| `POST /login` | Validate credentials, set session cookie |
| `POST /logout` | Clear session cookie |
| `GET /api/dashboard?date=` | Single-day scan results (`date` must be YYYY-MM-DD) |
| `GET /api/history?days=30` | 30-day summary |
| `GET /api/positions` | Live Alpaca positions (direct API call) |
| `GET /api/account` | Portfolio summary (equity, cash, P&L, realized P&L via FIFO) |
| `GET /api/live-trades` | Open DB trades merged with live Alpaca positions |
| `GET /api/closed-trades?date=&days=30` | Closed trades from DB |
| `GET /api/recent-sells` | Recent Alpaca sell fills (full closes + partial trims) with FIFO P&L |
| `GET /api/performance?days=90` | Win rate, avg hold, exit breakdown |
| `GET /api/config` | All config settings merged with defaults |
| `POST /api/config` | Write settings back to `.env` |
| `GET /api/symbols/fallback` | Returns custom override list or default 503 S&P 500 symbols |
| `POST /api/symbols/fallback` | Saves a custom symbol list to `data/universe_override.json` |
| `DELETE /api/symbols/fallback/reset` | Deletes the override file, restoring S&P 500 defaults |
| `GET /api/runner/status` | Whether runner.py daemon is currently running |
| `POST /api/runner/start` | Start runner.py daemon (accepts `{"dry_run": bool}`) |
| `POST /api/runner/stop` | Stop runner.py daemon |
| `POST /api/scan/start` | Run scanner.py immediately (accepts `{"execute": bool}`) |
| `GET /api/scan/output?offset=N` | Poll scanner subprocess output lines since offset |
| `GET /api/scan/next` | Next scan timestamp, runner status, market open/closed |

Every endpoint except `/login`, `/logout`, and `/static/*` requires a valid session cookie. See Security section below.

The web dashboard (`webapp/static/index.html`) has four tabs (each refreshes data on click):
- **Overview** — Portfolio KPIs → Open Positions + Trades Placed Today (inline) → Recently Closed & Trims
- **Today** — Stats bar → Trades Placed → Scanner Candidates (with Scan Now button)
- **Last 30 Days** — daily chart + summary table + closed trades
- **Config** — editable configuration

The header shows: **Logo | Regime badge | Scan countdown | Date picker | Refresh | Sign Out**. The scan countdown shows `Scanning…` / `Next scan X:XX` / `Market closed` depending on state.

### Scan Now button

Located in the **Scanner Candidates card header** on the Today tab, right side:
- **DRY RUN** (default, grey) — runs `scanner.py` immediately, streams output to a slide-in panel, no orders placed.
- **EXECUTE** (red, requires confirmation) — runs `scanner.py --execute`, placing real bracket orders.
- If the runner daemon is already active, the button shows **Stop Runner** and stops it instead.

### Config Tab

The Config tab exposes all system settings. Sections (in order):

- **Scanner Thresholds** — min score, min price, min volume, ATR multipliers, RSI zone bounds
- **Risk Management** — max portfolio risk %, max position size %, max open trades
- **Trade Setup** — partial exit R, partial exit %, trailing stop ATR multiplier
- **Bull Trap & Accumulation** — individual penalty/bonus weights
- **Signal Weights** — individual signal point allocations (must sum to 100)
- **Market Regime** — regime-aware toggle, override
- **Scanner Schedule** — scan interval, timeframe
- **Position Management Engine (PME)** — all PME thresholds and multipliers
- **Backtest Settings** — max hold days, slippage, initial capital
- **Trading Mode** — paper trading toggle, Alpaca base URL
- **Scanner Universe** — symbol chips, filter, add/remove, reset to S&P 500 defaults
- **WhatsApp Notifications** — phone, API key, test button

Alpaca API key and secret key are **not editable in the UI** — edit `.env` directly to change them.

Clicking "Save Configuration" POSTs to both `/api/config` and `/api/symbols/fallback` in parallel.

### Security

`webapp/app.py` uses **session-cookie authentication** (not HTTP Basic Auth):
- Set `DASHBOARD_USER` and `DASHBOARD_PASS` in `.env` to enable auth. If not set, auth is disabled (safe for local-only use).
- On successful login, a random 32-byte `a1t_sess` `HttpOnly; SameSite=lax` cookie is issued. The password is never stored in the browser.
- Sessions expire after **8 hours** server-side (in-memory — restarting the server clears all sessions). The browser idle timer logs out after **10 minutes of no activity**.
- **Rate limiting** — max 10 login attempts per IP per 5-minute window (HTTP 429).
- **Failed auth logging** — every failed login is written to `logs/auth.log` with timestamp and IP.
- **Security headers** — all responses include `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy`, `Referrer-Policy`, and `Cache-Control: no-store` on API endpoints.
- **Date validation** — `?date=` params on `/api/dashboard` and `/api/closed-trades` reject non-`YYYY-MM-DD` values with HTTP 400.
- Login page: `webapp/static/login.html`

### Symbol Universe Management

`data/universe.py` — `get_symbols()` first checks for `data/universe_override.json`. If the file exists it returns those symbols; otherwise it returns the hardcoded `_FALLBACK_SYMBOLS` list (503 S&P 500 tickers). This lets the Config tab persist a custom list without modifying source code.

- Override file is created/updated by `POST /api/symbols/fallback`
- Override file is deleted by `DELETE /api/symbols/fallback/reset`
- Absence of the file = use S&P 500 defaults (no override in effect)

`runner.py` — autonomous daemon. Polls Alpaca clock, sleeps until market open, scans 5 min after open, then rescans on `--interval` cadence. Writes logs to `logs/runner_YYYY-MM-DD.log`. On scan errors it retries at next interval; on unexpected exceptions it sleeps 60s and restarts the loop automatically.

### Regression Test Suite

`tests/test_regression.py` — 178 tests across 14 test classes. `tests/conftest.py` holds shared fixtures. `run_tests.py` is a convenience runner.

Test classes:
- `TestConfig` (9) — config/env loading
- `TestUniverse` (9) — symbol universe and override file
- `TestStore` (9) — SQLite persistence
- `TestBreakoutSignals` (13) — signal detection pipeline
- `TestBreakoutScorer` (10) — scoring engine
- `TestMarketRegime` (11) — regime classification
- `TestTradeSetup` (11) — trade setup calculation
- `TestRiskManager` (8) — risk approval logic
- `TestWebappAPI` (19) — REST API endpoints
- `TestE2EPipeline` (10) — full end-to-end pipeline
- `TestRunnerAndScanAPI` (11) — runner daemon and scan subprocess API
- `TestPositionManager` (12) — PME decision logic
- `TestPositionExecutor` (13) — PME execution (trim/exit/add)
- `TestSecurity` (13) — session auth, rate limiting, input validation, security headers

Key test fixtures (in `conftest.py`):
- `make_ohlcv()`, `make_breakout_df()`, `make_spy_df()`, `make_bull_spy_df()`, `make_bear_spy_df()` — OHLCV data generators
- `mock_alpaca` — MagicMock Alpaca client
- `temp_db` — monkeypatches `store.DB_PATH` to a temp file
- `temp_universe` — monkeypatches `universe._OVERRIDE_PATH` to a temp dir
- `api_client` — FastAPI `TestClient` with isolated DB, universe, and `.env` (auth disabled — `DASHBOARD_USER`/`PASS` not set)
- `auth_client` — same isolation but with auth enabled (used by `TestSecurity`)

### Server restart after code changes

If the server is already running and you change `webapp/app.py` or other modules, you must kill the old process and start a new one:

```powershell
# Find the PID listening on port 8000
netstat -ano | findstr :8000

# Kill it (replace 12345 with actual PID)
Stop-Process -Id 12345 -Force

# Start fresh
Start-Process python -ArgumentList "-m webapp.app" -NoNewWindow
```

### Backtest and optimiser

`backtest/engine.py` — event-driven. Signal fires on day T close → entry at T+1 open + slippage. Two-phase exit: Phase 1 (full stop or partial target), Phase 2 (trailing stop ratchet after partial fills). Timeout at `BACKTEST_MAX_HOLD_DAYS=20`.

`backtest/optimizer.py` — grid search over configurable parameter ranges, runs the backtest engine for each combination, returns ranked results.

### Windows Task Scheduler (automated daily trading)

Two scheduled tasks are active on this machine (Mon–Fri, Doha/AST UTC+3):

| Task | Time | Command |
|---|---|---|
| `A1TRADES Start` | 4:15 PM | `C:\Projects\ATrades\start_runner.bat` |
| `A1TRADES Stop` | 11:15 PM | `stop_runner.ps1` |

To recreate if deleted:
```powershell
schtasks /create /tn "A1TRADES Start" /sc weekly /d MON,TUE,WED,THU,FRI /st 16:15 /tr "C:\Projects\ATrades\start_runner.bat" /f
schtasks /create /tn "A1TRADES Stop"  /sc weekly /d MON,TUE,WED,THU,FRI /st 23:15 /tr "powershell -ExecutionPolicy Bypass -File C:\Projects\ATrades\stop_runner.ps1" /f
```

`stop_runner.ps1` kills the runner process by matching the command line — do not inline this in schtasks (escaping breaks). Tasks run as a Windows service and do not need a terminal open, but the PC must be on and not sleeping (`powercfg /change standby-timeout-ac 0`).

To expose the web dashboard publicly while running locally, use Cloudflare Tunnel:
```
cloudflared tunnel --url http://localhost:8000
```

### Windows notes

`scanner.py` calls `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at startup to prevent `cp1252` encoding crashes on Windows consoles. All print output uses ASCII only (no Unicode box-drawing characters).

yfinance may return MultiIndex columns on newer versions — code that calls yfinance directly must handle this: `if isinstance(df.columns, pd.MultiIndex): df.columns = [c[0].lower() for c in df.columns]`.

All times displayed in the web dashboard (`webapp/static/index.html`) use `timeZone: 'America/New_York'` — always show ET regardless of browser locale.
