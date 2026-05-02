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

## Architecture

### Signal → Score → Setup → Orders pipeline

`scanner.py` is the entry point for everything. One full scan pass:

1. **Universe** (`data/universe.py`) — returns the hardcoded list of 503 S&P 500 symbols.
2. **Market data** (`data/market_data.py`) — fetches OHLCV via yfinance.
3. **Regime gate** (`strategy/market_regime.py`) — classifies SPY into `BULL_TREND / SIDEWAYS / BEAR_TREND / HIGH_VOLATILITY` using ADX(14) and 200MA. Applies a `score_multiplier` to all candidates and raises the `min_score` floor in bad regimes.
4. **Signal detection** (`strategy/breakout_signals.py`) — `detect_all()` returns a `BreakoutSignals` dataclass per symbol with 9 factor signals. Symbols failing base filters (price, volume, missing data) return `None`.
5. **Accumulation** (`strategy/accumulation.py`) — OBV, CMF, up/down volume ratio, block days. Adds up to +15 pts.
6. **Bull trap detection** (`strategy/bull_trap.py`) — 5 warning signs; subtracts up to −40 pts from final score.
7. **Scorer** (`strategy/breakout_scorer.py`) — `BreakoutScorer.score()` produces a 0–100 confidence score.
8. **Trade setup** (`risk/trade_setup.py`) — `calculate_setup()` takes a `BreakoutSignals` + score and returns a `TradeSetup` with stop, target, sizing. Returns `None` when ATR=0 or risk-per-share is invalid.
9. **Order execution** (`execution/bracket_orders.py`) — 3 separate Alpaca orders: market buy (all shares), GTC limit sell (partial), GTC trailing stop sell (remainder).

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

`webapp/app.py` — FastAPI server. Three API endpoints: `/api/dashboard?date=` (single day), `/api/history?days=30` (30-day summary), `/api/positions` (live Alpaca positions). Static files served from `webapp/static/`.

`runner.py` — autonomous daemon. Polls Alpaca clock, sleeps until market open, scans 5 min after open, then rescans on `--interval` cadence. Writes logs to `logs/runner_YYYY-MM-DD.log`.

### Backtest and optimiser

`backtest/engine.py` — event-driven. Signal fires on day T close → entry at T+1 open + slippage. Two-phase exit: Phase 1 (full stop or partial target), Phase 2 (trailing stop ratchet after partial fills). Timeout at `BACKTEST_MAX_HOLD_DAYS=20`.

`backtest/optimizer.py` — grid search over configurable parameter ranges, runs the backtest engine for each combination, returns ranked results.

### Windows Task Scheduler (automated daily trading)

`runner.py` is designed to run unattended. Schedule it to start before US market open and stop after close. Times below are IST (UTC+5:30); adjust if timezone differs.

```powershell
# Start runner Mon–Fri at 4:15 PM AST (15 min before US market open)
schtasks /create /tn "ATrades Start" /sc weekly /d MON,TUE,WED,THU,FRI /st 16:15 /tr "python C:\projects\atrades\runner.py" /f

# Stop runner Mon–Fri at 11:15 PM AST (15 min after US market close)
schtasks /create /tn "ATrades Stop" /sc weekly /d MON,TUE,WED,THU,FRI /st 23:15 /tr "powershell -ExecutionPolicy Bypass -File C:\projects\atrades\stop_runner.ps1" /f
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
