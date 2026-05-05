# A1TRADES — Functional Specification Document

**Version:** 2.0  
**Date:** 2026-05-05  
**Repository:** https://github.com/aftabk1/atrades (private)

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Architecture](#2-architecture)
3. [Configuration (`config.py`)](#3-configuration)
4. [Symbol Universe (`data/universe.py`)](#4-symbol-universe)
5. [Market Data (`data/market_data.py`)](#5-market-data)
6. [Market Regime (`strategy/market_regime.py`)](#6-market-regime)
7. [Signal Detection (`strategy/breakout_signals.py`)](#7-signal-detection)
8. [Accumulation Detection (`strategy/accumulation.py`)](#8-accumulation-detection)
9. [Bull Trap Detection (`strategy/bull_trap.py`)](#9-bull-trap-detection)
10. [Scoring Engine (`strategy/breakout_scorer.py`)](#10-scoring-engine)
11. [Trade Setup & Risk (`risk/trade_setup.py`)](#11-trade-setup--risk)
12. [Order Execution (`execution/bracket_orders.py`)](#12-order-execution)
13. [Position Monitor (`execution/position_monitor.py`)](#13-position-monitor)
14. [Scanner (`scanner.py`)](#14-scanner)
15. [Autonomous Runner (`runner.py`)](#15-autonomous-runner)
16. [Persistence Layer (`data/store.py`)](#16-persistence-layer)
17. [Web Dashboard (`webapp/app.py` + `index.html`)](#17-web-dashboard)
18. [Backtest Engine (`backtest/engine.py`)](#18-backtest-engine)
19. [Parameter Optimizer (`backtest/optimizer.py`)](#19-parameter-optimizer)
20. [Broker Client (`broker/alpaca_client.py`)](#20-broker-client)
21. [End-to-End Flow](#21-end-to-end-flow)
22. [Risk Controls Summary](#22-risk-controls-summary)

---

## 1. System Overview

A1TRADES is a fully automated momentum breakout trading system for US equities. It scans the S&P 500 (or a custom symbol list), identifies high-probability breakout setups using a multi-factor scoring model, sizes positions according to a fixed-risk framework, and executes three-leg bracket orders via the Alpaca brokerage API.

**What it trades:** Daily-close breakouts from consolidation bases into new highs, backed by institutional accumulation. Long-only. No shorts, options, or intraday scalps.

**Key capabilities:**
- Real-time scan of up to 503 symbols with full signal pipeline in ~3 minutes
- Market regime gate that adjusts or suspends scanning in bad market conditions
- Three-way entry gate: 20-day breakout, or tight 10-day breakout with volume + RS, or earnings gap-up with volume
- Weighted 9-factor scoring system (0–100), configurable through the web UI
- Institutional accumulation composite (+15 pts max) and bull-trap penalty (−32 pts max)
- Automated three-leg bracket order: market buy → hard stop → GTC partial limit → trailing stop
- Intraday position monitor with orphan protection, circuit breaker, and trailing stop ratchet
- Event-driven backtest engine and grid-search parameter optimizer
- Web dashboard for live monitoring, config management, and scan control

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        runner.py                            │
│   (autonomous daemon — waits for market open, drives loop)  │
└────────────────────────┬────────────────────────────────────┘
                         │ calls
┌────────────────────────▼────────────────────────────────────┐
│                       scanner.py                            │
│   BreakoutScanner.scan()  →  print_table()  →  to_json()   │
└──┬─────────────────────────────────────────────────────────┘
   │
   ├─ data/universe.py          → symbol list
   ├─ data/market_data.py       → OHLCV via yfinance / Alpaca
   ├─ strategy/market_regime.py → SPY regime classification
   ├─ strategy/breakout_signals.py → 9 signals + 3-way gate
   ├─ strategy/accumulation.py → OBV / CMF / UpDownVol / InstDays
   ├─ strategy/bull_trap.py    → 5 false-breakout checks
   ├─ strategy/breakout_scorer.py → 0–100 weighted score
   ├─ risk/trade_setup.py      → stop / target / sizing
   ├─ execution/bracket_orders.py → 3 Alpaca orders
   ├─ execution/position_monitor.py → sync / ratchet / CB
   └─ data/store.py            → SQLite persistence

┌─────────────────────────────────────────────────────────────┐
│                      webapp/app.py                          │
│   FastAPI — reads store.py, calls scanner on demand,        │
│   exposes REST API to index.html dashboard                  │
└─────────────────────────────────────────────────────────────┘
```

**Data flow per scan cycle:**
1. `StockUniverse.get_symbols()` → symbol list
2. `MarketDataClient.get_daily_bars()` → 252 days of OHLCV
3. `detect_regime(spy_df)` → `MarketRegime`
4. For each symbol: `detect_all()` → `BreakoutSignals` (or `None` if gate fails)
5. `BreakoutScorer.score()` → raw 0–100 float
6. Multiply by `regime.score_multiplier`, compare to `effective_min_score`
7. `calculate_setup()` → `TradeSetup` (stop, target, shares)
8. Collect `_build_candidate()` dicts, sort by score, take top 5
9. If `--execute`: `BracketOrderExecutor.place()` for each candidate within slot limit
10. `save_scan()` → SQLite

---

## 3. Configuration

**File:** `config.py`  
**Source:** `.env` file in project root (loaded via `python-dotenv` with `override=True`, path pinned to `config.py`'s directory to work regardless of working directory).

All parameters can be changed at runtime through the web dashboard Config tab and persist immediately to `.env`. The running process reloads config via `importlib.reload(config)` after each save.

### Alpaca Credentials

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ALPACA_API_KEY` | str | `""` | API key |
| `ALPACA_SECRET_KEY` | str | `""` | Secret key |
| `ALPACA_BASE_URL` | str | `https://paper-api.alpaca.markets` | Paper or live endpoint |
| `IS_PAPER` | bool | `true` | Paper trading mode |

### Portfolio Risk Limits

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `MAX_POSITION_SIZE` | float | `0.10` | Max portfolio fraction per position (10%) |
| `MAX_PORTFOLIO_RISK` | float | `0.01` | Max loss per trade as fraction of portfolio (1%) |
| `MAX_CONCURRENT_TRADES` | int | `4` | Max simultaneously open positions |
| `MAX_DAILY_LOSS_PCT` | float | `0.04` | Circuit breaker: halt if portfolio drops 4% in a day |

### Scanner Base Filters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `BREAKOUT_MIN_PRICE` | float | `25.0` | Minimum close price to consider a symbol |
| `BREAKOUT_MIN_AVG_VOLUME` | int | `1,000,000` | Minimum 20-day average daily volume |
| `BREAKOUT_MIN_SCORE` | float | `60.0` | Minimum score threshold for a trade to qualify |

### Signal Thresholds

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `BREAKOUT_VOLUME_SURGE_MULT` | float | `1.5` | Today's volume must be ≥ N× the 20-day average |
| `BREAKOUT_RSI_LOW` | float | `55.0` | Lower bound of RSI momentum zone |
| `BREAKOUT_RSI_HIGH` | float | `70.0` | Upper bound of RSI momentum zone |
| `BREAKOUT_ATR_EXPANSION_THRESHOLD` | float | `1.2` | ATR(5) / ATR(20) ratio for volatility expansion |
| `BREAKOUT_CONSOLIDATION_LOOKBACK` | int | `15` | Bars to measure base tightness |
| `BREAKOUT_HIGHER_LOWS_LOOKBACK` | int | `15` | Bars to detect ascending swing lows |

### Trade Setup Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `BREAKOUT_ATR_STOP_MULT` | float | `2.0` | Stop = entry − N×ATR(14) |
| `BREAKOUT_MAX_STOP_PCT` | float | `0.20` | Stop floor: never below 80% of entry |
| `BREAKOUT_SUPPORT_LOOKBACK` | int | `10` | Bars for swing-low support calculation |
| `BREAKOUT_RR_RATIO` | float | `2.0` | Partial target at N× risk-per-share |
| `PARTIAL_EXIT_R` | float | `2.0` | R-multiple for partial exit |
| `PARTIAL_EXIT_PCT` | float | `0.50` | Fraction of position to exit at partial target (50%) |
| `TRAIL_ATR_MULT` | float | `2.0` | Trailing stop distance = N×ATR(14) |
| `GAP_UP_THRESHOLD` | float | `0.08` | Gap-up trigger: today open ≥ 8% above prior close |

### Signal Score Weights (9 base signals, must sum to 100)

| Parameter | Default | Signal |
|-----------|---------|--------|
| `SCORE_VOLUME_SURGE` | `20.0` | Volume surge |
| `SCORE_BREAKOUT_20D` | `16.0` | 20-day high breakout |
| `SCORE_RELATIVE_STRENGTH` | `12.0` | RS vs SPY |
| `SCORE_RSI_ZONE` | `12.0` | RSI in momentum zone |
| `SCORE_BREAKOUT_50D` | `12.0` | 50-day high breakout |
| `SCORE_ATR_EXPANSION` | `8.0` | ATR expansion |
| `SCORE_CONSOLIDATION` | `8.0` | Base consolidation |
| `SCORE_HIGHER_LOWS` | `8.0` | Higher swing lows |
| `SCORE_EARNINGS_PROXIMITY` | `4.0` | Earnings within ±7 days |

### Adjustments (applied on top of the 100-pt base, not included in base total)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `SCORE_ACCUM_MAX_BONUS` | `12.0` | Max accumulation bonus pts |
| `SCORE_TRAP_MAX_PENALTY` | `32.0` | Max bull-trap penalty pts |

### Accumulation & Trap

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `ACCUM_LOOKBACK_DAYS` | int | `20` | Window for accumulation detection |
| `BULL_TRAP_SCORE_THRESHOLD` | float | `40.0` | Trap score ≥ this = flagged as trap |

### Market Regime

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `REGIME_AWARE_SCANNING` | bool | `true` | Enable regime gate and score adjustment |
| `REGIME_OVERRIDE` | str | `""` | Force a regime: `BULL_TREND`, `SIDEWAYS`, etc. |

### Runner / Scheduler

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `SCANNER_INTERVAL_MINUTES` | int | `5` | Runner re-scan interval |
| `SCAN_MODE` | str | `custom` | Unused placeholder |

### Backtest

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `BACKTEST_MAX_HOLD_DAYS` | int | `20` | Timeout after N days |
| `BACKTEST_SLIPPAGE_PCT` | float | `0.0005` | Slippage: 0.05% added to entry |
| `BACKTEST_INITIAL_CAPITAL` | float | `100,000` | Starting capital |

---

## 4. Symbol Universe

**File:** `data/universe.py`  
**Class:** `StockUniverse`

`get_symbols()` returns the active symbol list. Logic:

1. Check for `data/universe_override.json`
   - If present: load and return that list (custom override, set via Config tab)
2. If absent: return `_FALLBACK_SYMBOLS` — hardcoded list of 503 S&P 500 tickers

The override file is created/updated via `POST /api/symbols/fallback` and deleted via `DELETE /api/symbols/fallback/reset`.

---

## 5. Market Data

**File:** `data/market_data.py`  
**Class:** `MarketDataClient`

### `get_daily_bars(symbols, days=252) → dict[str, pd.DataFrame]`

Fetches daily OHLCV for a list of symbols. Two-tier strategy:

1. **Alpaca** (primary): batch requests of 3 symbols each. Requires paid market data subscription. Returns raw Alpaca OHLCV bars.
2. **yfinance** (fallback): used automatically when Alpaca returns a subscription error. Fetches all remaining symbols in a single multi-ticker call. Handles `MultiIndex` columns (common on newer yfinance): `if isinstance(df.columns, pd.MultiIndex): df.columns = [c[0].lower() for c in df.columns]`

Returns a `dict[symbol → DataFrame]` with lowercase columns: `open, high, low, close, volume`. Symbols with insufficient data (<60 bars) are excluded.

### `get_spy_data(days=252) → pd.DataFrame`

Same two-tier fetch for SPY only. Used for regime detection and relative strength calculation.

### `get_earnings_date(symbol) → datetime | None`

Fetches the next earnings date via yfinance `Ticker.calendar`. Returns `None` on any error. Used to trigger the `earnings_proximity` signal.

---

## 6. Market Regime

**File:** `strategy/market_regime.py`  
**Entry point:** `detect_regime(spy_df) → MarketRegime`

Classifies the current market environment from SPY daily data. Requires ≥60 bars; defaults to SIDEWAYS with a warning if data is insufficient.

### Metrics Computed

| Metric | Calculation |
|--------|-------------|
| `adx` | ADX(14) using Wilder EMA smoothing on True Range and ±DM |
| `spy_slope_20d` | `(close[-1] / close[-21] − 1) × 100` |
| `spy_slope_50d` | `(close[-1] / close[-51] − 1) × 100` |
| `above_200ma` | `close[-1] > rolling_mean(close, 200)` |
| `realized_vol_20d` | `std(daily_returns[-20:]) × √252 × 100` |

### Classification Logic (priority order)

```
if realized_vol > 30%              → HIGH_VOLATILITY
elif not above_200ma and slope_20d < -3%  → BEAR_TREND
elif above_200ma and ADX ≥ 25 and slope_20d ≥ +2%  → BULL_TREND
else                               → SIDEWAYS
```

### Regime Effects on Scanner

| Regime | Score Multiplier | Min Score Override | Scan Recommended |
|--------|:-:|:-:|:-:|
| `BULL_TREND` | ×1.00 | 40 | ✅ Yes |
| `SIDEWAYS` | ×0.80 | 52 | ✅ Yes (with caution) |
| `BEAR_TREND` | ×0.55 | 62 | ❌ No |
| `HIGH_VOLATILITY` | ×0.40 | 70 | ❌ No |

`effective_min_score = max(config.BREAKOUT_MIN_SCORE, regime.min_score_override)`

---

## 7. Signal Detection

**File:** `strategy/breakout_signals.py`  
**Entry point:** `detect_all(symbol, df, spy_df, earnings_date, fast=False) → BreakoutSignals | None`

Returns `None` (symbol rejected) at any of the following gates. Otherwise returns a fully populated `BreakoutSignals` dataclass.

### Gate 1 — Data Sufficiency

```python
if df is None or len(df) < 60:
    return None
```

Minimum 60 bars of OHLCV history required for reliable signal computation.

### Gate 2 — Base Filters

```python
price      = close[-1]
avg_volume = mean(volume[-21:-1])
if price < BREAKOUT_MIN_PRICE or avg_volume < BREAKOUT_MIN_AVG_VOLUME:
    return None
```

Rejects penny stocks and illiquid names before computing any signals.

### Gate 3 — Three-Way Entry Gate

The mandatory breakout confirmation. Symbol must satisfy at least one of:

**Trigger A — 20-day high breakout (classic)**
```python
trigger_a = close[-1] > max(close[-21:-1])
```

**Trigger B — Tight thrust (10-day + volume + RS)**
```python
trigger_b = (close[-1] > max(close[-11:-1]))        # 10-day breakout
         and (today_vol / avg_vol_20 >= VOLUME_SURGE_MULT)  # volume surge
         and (stock_20d_return - spy_20d_return > 0)         # RS positive
```
This catches shorter-base thrusts with institutional volume and market leadership confirmation.

**Trigger C — Gap-up breakout**
```python
trigger_c = (open[-1] / close[-2] - 1 >= GAP_UP_THRESHOLD)  # gap ≥ 8%
         and (today_vol / avg_vol_20 >= VOLUME_SURGE_MULT)    # volume surge
```
Catches earnings/news-driven gap-up breakouts with above-average volume confirmation.

```python
if not (trigger_a or trigger_b or trigger_c):
    return None
```

**Note:** `volume_surge` and `relative_strength` are computed before this gate (they are needed for B and C evaluation), then reused in the score computation after the gate passes.

### Signals Computed (after gate passes)

All remaining signals are optional — they add or subtract points but cannot reject a symbol.

#### `breakout_20d` — 20-Day High Breakout
```
triggered = close[-1] > max(close[-21:-1])
value     = (close[-1] - prior_high) / prior_high × 100   (% above 20d high)
```
Points scale with percentage above the high: `min(max_pts × 0.5 + pct/3 × max_pts × 0.5, max_pts)`

#### `breakout_10d` — 10-Day High Breakout
Same formula with a 10-bar window. Not separately scored; only used in the entry gate.

#### `breakout_50d` — 50-Day High Breakout
```
triggered = close[-1] > max(close[-51:-1])
```
Full points when triggered; 0 otherwise.

#### `consolidation` — Tight Base
```
prior_close = close[-(n+1):-1]   (exclude today)
daily_vol   = std(pct_change(prior_close))
triggered   = daily_vol < BREAKOUT_CONSOLIDATION_DAILY_VOL  (default 0.015 = 1.5%)
```
Low day-to-day return volatility over the prior 15 bars signals price coiling in a base before breakout.

#### `higher_lows` — Ascending Swing Lows
```
lows = df["low"][-(n+1):-1]
swing_lows = [lows[i] for i where lows[i] < lows[i-1] and lows[i] < lows[i+1]]
triggered  = all swing_lows ascending
```
Detects that each successive low is higher than the previous, indicating accumulating demand.

#### `volume_surge` — Volume Expansion
```
ratio     = volume[-1] / mean(volume[-21:-1])
triggered = ratio >= BREAKOUT_VOLUME_SURGE_MULT
```
Points scale with ratio: `min((ratio - 1.0) / 2.0 × max_pts, max_pts)`

#### `rsi_zone` — Momentum Zone
```
rsi_val   = RSI(14, close[-1])
triggered = BREAKOUT_RSI_LOW ≤ rsi_val ≤ BREAKOUT_RSI_HIGH   (default 55–70)
```
The 55–70 zone means momentum is strong but not yet overbought. Full points if in zone; 0 otherwise.

#### `relative_strength` — vs SPY
```
sym_ret = close[-1] / close[-21] - 1   (20-day return)
spy_ret = spy_close[-1] / spy_close[-21] - 1
rs      = (sym_ret - spy_ret) × 100
triggered = rs > 0
```
Points scale with outperformance: `min(rs / 10.0 × max_pts, max_pts)`

#### `atr_expansion` — Volatility Expanding
```
atr5  = mean(TR[-5:])
atr20 = mean(TR[-20:])
ratio = atr5 / atr20
triggered = ratio > BREAKOUT_ATR_EXPANSION_THRESHOLD  (default 1.2)
```
Short-term ATR expanding above long-term baseline confirms the breakout has energy behind it. Full points if triggered; 0 otherwise.

#### `earnings_proximity` — Catalyst Window
```
days_off = (earnings_date - last_bar_date).days
triggered = -5 ≤ days_off ≤ 5
```
Bonus signal when within ±5 days of earnings.

#### `gap_pct` — Gap Measurement
```
gap_pct = open[-1] / close[-2] - 1
```
Not scored directly; stored on the signal for gate C evaluation and dashboard display.

### After gate: enhancement modules
```python
sig.accumulation = detect_accumulation(df, lookback=ACCUM_LOOKBACK_DAYS)
sig.bull_trap    = detect_bull_trap(df)
```
Skipped when `fast=True` (used in backtest optimizer).

---

## 8. Accumulation Detection

**File:** `strategy/accumulation.py`  
**Entry point:** `detect_accumulation(df, lookback=20) → AccumulationSignals`

Identifies institutional buying signatures in the **pre-breakout** bars (`df.iloc[-(lookback+1):-1]` — today excluded). Returns `AccumulationSignals` with `composite_score` in [0, 1] = fraction of 4 sub-signals that triggered.

### Sub-signals

#### OBV Trend
```
direction = sign(close.diff())
obv = cumsum(direction × volume)
fast = SMA(obv, 5)
slow = SMA(obv, 20)
triggered = fast > slow
spread = (fast - slow) / std(obv)   # normalised in σ units
```
OBV fast MA crossing above slow MA = short-term volume is flowing into the stock.

#### Chaikin Money Flow
```
MFM = ((close - low) - (high - close)) / (high - low)   # in [-1, +1]
MFV = MFM × volume
CMF = sum(MFV, 20) / sum(volume, 20)
triggered = CMF > 0.05
```
CMF > +0.05 indicates 20-day net buying pressure exceeds selling pressure.

#### Up/Down Volume Ratio
```
up_vol   = sum(volume on days where close ≥ open, last 10 bars)
down_vol = sum(volume on days where close < open, last 10 bars)
ratio    = up_vol / down_vol
triggered = ratio ≥ 1.5
```
More capital traded on strength days than weakness days.

#### Institutional Buying Days
```
avg_vol    = mean(volume, all bars)
inst_mask  = (close > open) AND (volume > 1.5 × avg_vol)
count      = sum(inst_mask, last 20 bars, excl. today)
triggered  = count ≥ 3
```
At least 3 high-volume up-close bars = repeated block buying.

### Composite Score
```
composite_score = (triggered_count) / 4.0   → range [0, 0.25, 0.50, 0.75, 1.0]
```

---

## 9. Bull Trap Detection

**File:** `strategy/bull_trap.py`  
**Entry point:** `detect_bull_trap(df) → BullTrapResult`

Evaluates today's breakout bar for false-breakout warning signs. Returns `BullTrapResult` with `trap_score` in [0, 100] and `is_trap = trap_score ≥ 40`.

### Warning Signs (weights sum to 100)

| Check | Weight | Trigger Condition |
|-------|:------:|-------------------|
| `weak_close` | 30 | Close in lower 35% of today's range: `(close - low) / (high - low) < 0.35` |
| `prior_failures` | 25 | ≥2 prior attempts at this price level reversed within 5 bars (60-bar lookback, ±2% proximity) |
| `resistance_zone` | 20 | ≥4 prior daily highs within 1.5% of current close (60-bar lookback) |
| `rsi_divergence` | 15 | Price higher than 10 bars ago but RSI lower (momentum not confirming) |
| `narrow_bar` | 10 | Today's range < 70% of ATR(14): `(high - low) / ATR(14) < 0.70` |

```
trap_score = sum of weights for triggered checks
is_trap    = trap_score >= 40
```

Triggered trap warnings are collected and displayed in the scanner output and dashboard.

---

## 10. Scoring Engine

**File:** `strategy/breakout_scorer.py`  
**Class:** `BreakoutScorer`

### `score(signals) → float`

Produces a single confidence score in [0, 100] using a three-phase pipeline:

**Phase 1 — Base score (9 factors)**
```
pts_dict = {factor: config.SCORE_{FACTOR} for each of the 9 signals}
base     = min(Σ factor_pts(signal, factor, pts_dict), 100.0)
```

Factor-level scoring rules:
- `volume_surge`: `min((ratio - 1.0) / 2.0 × max_pts, max_pts)` — scaled with ratio above threshold
- `breakout_20d`: `min(max_pts × 0.5 + pct/3.0 × max_pts × 0.5, max_pts)` — scaled with % above 20d high
- `relative_strength`: `min(rs / 10.0 × max_pts, max_pts)` — scaled with RS magnitude
- All other signals: 0 or full `max_pts` (binary: triggered = full points, not triggered = 0)

**Phase 2 — Accumulation bonus**
```
bonus = signals.accumulation.composite_score × SCORE_ACCUM_MAX_BONUS
```
`composite_score` is 0/4, 1/4, 2/4, 3/4, or 4/4. Max bonus = `SCORE_ACCUM_MAX_BONUS` (default 12).

**Phase 3 — Bull trap penalty**
```
penalty = signals.bull_trap.trap_score × (SCORE_TRAP_MAX_PENALTY / 100.0)
```
`trap_score` is in [0, 100]. Max penalty = `SCORE_TRAP_MAX_PENALTY` (default 32).

**Final:**
```
score = clamp(base + bonus - penalty, 0, 100)
```

### `breakdown(signals) → dict`

Returns per-factor point allocation plus `accum_bonus` and `trap_penalty` for display in the scanner output and dashboard.

---

## 11. Trade Setup & Risk

**File:** `risk/trade_setup.py`  
**Entry point:** `calculate_setup(signals, score, portfolio_value) → TradeSetup | None`

Returns `None` when ATR = 0, entry ≤ 0, or stop ≥ entry.

### Stop Loss

```
atr_stop     = entry - BREAKOUT_ATR_STOP_MULT × ATR(14)       # 2×ATR below entry
support_stop = swing_low × 0.995                               # 0.5% below 10-day swing low
stop_floor   = entry × (1 - BREAKOUT_MAX_STOP_PCT)            # never below 80% of entry

stop = max(min(atr_stop, support_stop), stop_floor)
```

Takes the **wider** (more conservative) of ATR stop and support stop, then enforces the floor. This means the stop is placed at whichever level gives the position more room.

### Partial Target

```
target = min(entry + PARTIAL_EXIT_R × risk_per_share, entry × 1.5)
```

2R target capped at 1.5× entry to avoid unrealistic targets on low-risk-per-share setups.

### Trailing Stop Distance

```
trail_atr = TRAIL_ATR_MULT × ATR(14)   (default 2×ATR)
```

This is the GTC trail distance placed after partial exit fills.

### Position Sizing

```
dollar_risk_budget = portfolio_value × MAX_PORTFOLIO_RISK    (1% of portfolio)
shares_by_risk     = int(dollar_risk_budget / risk_per_share)

position_cap       = portfolio_value × MAX_POSITION_SIZE     (10% of portfolio)
shares_by_size     = int(position_cap / entry)

shares = max(min(shares_by_risk, shares_by_size), 1)
```

The smaller of risk-based and size-based limits is used, ensuring neither the dollar risk nor the position size limit is breached.

### Share Split

```
partial_shares = max(int(shares × PARTIAL_EXIT_PCT), 1)   (50% rounded down)
trail_shares   = shares - partial_shares
```

### Output Fields

| Field | Description |
|-------|-------------|
| `entry_price` | Current price (signal bar close) |
| `stop_loss` | Calculated stop |
| `target_price` | Partial exit limit price |
| `trail_atr` | Trailing stop dollar distance |
| `shares` | Total shares to buy |
| `partial_shares` | Shares in GTC limit order |
| `trail_shares` | Shares in trailing stop after partial |
| `dollar_risk` | `shares × risk_per_share` |
| `dollar_reward` | `partial_shares × (target - entry)` |
| `risk_reward` | `(target - entry) / risk_per_share` |
| `portfolio_pct` | `shares × entry / portfolio × 100` |

---

## 12. Order Execution

**File:** `execution/bracket_orders.py`  
**Class:** `BracketOrderExecutor`

### `place(setup) → dict | None`

Executes a three-leg bracket for one `TradeSetup`. Returns `None` on failure.

**Pre-checks:**
- Market must be open (`is_market_open()`)
- `setup.shares >= 1`

**Leg 1 — Market Buy**
```
MarketOrderRequest(symbol, qty=shares, side=BUY, tif=DAY)
```
Submits immediately at market. Polls for fill status every 2 seconds up to 60-second timeout.

**Fill polling:**
- `FILLED` → record `fill_price` and `fill_ts`, proceed to exits
- `CANCELED | EXPIRED | REJECTED` → abort, return partial result
- Timeout (60s) → log error, return without exits (position needs manual review)

**Leg 2 — Hard Stop (all shares)**
```
StopOrderRequest(symbol, qty=shares, side=SELL, tif=GTC, stop_price=stop_loss)
```
Placed immediately after fill confirmation. Protects full position while the partial limit is working.

**Leg 3 — Partial Limit Sell**
```
LimitOrderRequest(symbol, qty=partial_shares, side=SELL, tif=GTC, limit_price=target_price)
```
GTC limit at the 2R target. When this fills, the position monitor upgrades the hard stop to a trailing stop.

**Return dict fields:** `buy_order_id`, `stop_order_id`, `partial_order_id`, `trail_order_id` (null initially), `symbol`, `shares`, `partial_shares`, `trail_shares`, `entry`, `fill_price`, `fill_ts`, `stop_loss`, `partial_target`, `trail_atr`, `score`

---

## 13. Position Monitor

**File:** `execution/position_monitor.py`

Four functions called by `runner.py` to manage live positions.

### `sync_open_trades(client)`

Reconciles DB open trades against live Alpaca state. Called before each scan and every 10 minutes between scans.

For each `open` or `partial_exit` trade:

1. **Fill recording**: If `fill_price` is null in DB, fetch buy order and record actual fill if `FILLED`.

2. **Position closed check**: If `get_position(symbol)` returns `None`, call `_handle_closed_position()` — iterates stop/trail/partial order IDs, finds the `FILLED` one, calls `close_trade()` with reason. If no filled order found, closes with `reason="unknown"`.

3. **Partial exit upgrade** (status=`open`): If partial limit order is `FILLED`, call `_upgrade_stop_to_trail()`:
   - Cancel the hard stop order
   - Place `TrailingStopOrderRequest(symbol, qty=trail_shares, side=SELL, tif=GTC, trail_price=trail_atr)`
   - Call `upgrade_to_trailing()` in DB (sets `trail_order_id`, clears `stop_order_id`, sets `status='partial_exit'`)

4. **Trailing stop fired** (status=`partial_exit`): If trail order is `FILLED`, call `close_trade()` with `reason="trailing_stop"`.

### `ratchet_trailing_stops(client)`

Called end-of-day. For each `partial_exit` trade:

1. Fetch latest daily bars via yfinance
2. Compute `new_trail_dist = 2 × ATR(14)`
3. If `new_trail_dist < current_trail_price` (trail is tightening = gains locked):
   - Cancel old trail order
   - Submit new `TrailingStopOrderRequest` with tighter distance
   - Update DB via `upgrade_to_trailing()`

Ratchet logic: only tightens, never widens. Locks in profits progressively.

### `check_circuit_breaker(client) → bool`

Returns `True` if trading should halt for the remainder of the day.

- Records `start_of_day_value` once per calendar day on first call
- On subsequent calls: `daily_loss_pct = (start - current) / start`
- If `daily_loss_pct >= MAX_DAILY_LOSS_PCT`: log warning, return `True`

When circuit breaker is active, `runner.py` temporarily sets `scanner._execute = False` for that scan cycle but still runs and saves the scan.

### `reconcile_orphans(client)`

Safety net that ensures every open Alpaca position has at least one GTC sell order covering it.

1. Fetch all open Alpaca positions
2. Fetch all open GTC sell orders, sum `qty` per symbol
3. For each position where `covered_qty < position_qty`:
   - Compute emergency stop price: DB `stop_loss` if available, else `current_price × 0.95`
   - Place `StopOrderRequest` for uncovered shares at that stop price
   - Log warning

Runs every 10 minutes between scans when execute mode is active.

---

## 14. Scanner

**File:** `scanner.py`  
**Class:** `BreakoutScanner`

### `scan(symbols=None) → list[dict]`

Main entry point. Returns up to `_TOP_N` (default 5) candidates sorted by descending score.

**Full pipeline:**
1. Build symbol list (`symbols` arg or `StockUniverse.get_symbols()`)
2. `MarketDataClient.get_daily_bars(symbols, days=252)`
3. `MarketDataClient.get_spy_data(days=252)`
4. `detect_regime(spy_data)` → `MarketRegime`
5. Compute `effective_min_score = max(config.BREAKOUT_MIN_SCORE, regime.min_score_override)`
6. `broker.get_portfolio_value()` and `broker.get_all_positions()`
7. For each symbol:
   - Skip if in `open_positions`
   - `detect_all(symbol, df, spy_data, earnings_date)` → skip if `None`
   - `scorer.score(signals)` → `raw_score`
   - `score = raw_score × regime.score_multiplier` (if regime-aware)
   - Skip if `score < effective_min_score`
   - `calculate_setup(signals, score, portfolio_value)` → skip if `None`
   - Append `_build_candidate(signals, setup, scorer, regime)` dict
8. Sort by score descending, take top 5
9. If `execute=True` and candidates found: `_place_orders(top, open_positions)`
   - `slots = MAX_CONCURRENT_TRADES - len(open_positions)`
   - Skip trap-flagged candidates
   - Call `BracketOrderExecutor.place()` for up to `slots` candidates
   - Save placed orders to DB
10. Return top candidates

### CLI Modes

| Flag | Behaviour |
|------|-----------|
| *(none)* | Single scan, table output, no orders |
| `--symbols SYM...` | Scan only the listed tickers |
| `--execute` | Scan + place Alpaca bracket orders |
| `--intraday` | Scan every `--interval` minutes while market is open |
| `--no-regime` | Bypass regime gate (testing) |
| `--json` | Additional JSON output |
| `--backtest` | Run `BacktestEngine.run()` on up to 50 symbols |
| `--optimize` | Run `ParameterOptimizer.run()` grid search |
| `--metric M` | Optimiser ranking metric: `sharpe`, `win_rate`, `profit_factor`, `total_return` |

### Candidate Dict (`_build_candidate`)

| Key | Description |
|-----|-------------|
| `symbol` | Ticker |
| `score` | Final adjusted score |
| `entry` | Entry price |
| `stop` | Stop loss |
| `target` | Partial exit target |
| `trail_atr` | Trailing stop distance |
| `shares` | Total shares |
| `partial_shares` | Shares at limit |
| `trail_shares` | Shares at trail |
| `dollar_risk` | Position risk in $ |
| `dollar_reward` | Partial-exit reward in $ |
| `risk_reward` | R:R ratio |
| `portfolio_pct` | % of portfolio |
| `gap_pct` | Today open vs prior close % |
| `volume_ratio` | Volume vs 20d avg |
| `rsi` | RSI(14) |
| `rs_vs_spy` | 20d RS vs SPY % |
| `breakout_20d/10d/50d` | bool |
| `accum_score` | Accumulation composite (0–1) |
| `is_trap` | Bull trap flag |
| `trap_score` | 0–100 |
| `trap_warnings` | List of warning strings |
| `accum_detail` | OBV / CMF / UD-vol / inst-days descriptions |
| `regime` | Regime state string |
| `score_breakdown` | Per-factor pts dict |
| `signals` | Per-signal description strings |

---

## 15. Autonomous Runner

**File:** `runner.py`

A blocking daemon designed to run unattended, typically scheduled via Windows Task Scheduler.

### Startup

1. Parse `--interval` (default 60 min) and `--dry-run` flags
2. Configure loguru to `stderr` + rotating daily file (`logs/runner_YYYY-MM-DD.log`, 30-day retention)
3. If live trading (not paper, not dry-run): print 10-second warning
4. `init_db()` and create `logs/` directory
5. Create `BreakoutScanner(execute=not dry_run)`

### Main Loop

```
while True:
    _wait_for_open()   → polls Alpaca clock, sleeps adaptively
    run_session()      → full trading session
```

Catches all exceptions, logs them, sleeps 60s, and restarts.

### `_wait_for_open(scanner)`

Adaptive sleep based on time to next market open:
- `> 1 hour` away: sleep 1 hour
- `5–60 min` away: sleep 5 min
- `< 5 min` away: sleep 30 seconds (poll every 30s)
- Market open: return immediately

### `run_session(scanner, rescan_interval)`

1. Sleep `SCAN_AFTER_OPEN_MINS` (5 min) after open for prices to settle
2. Loop until market closes:
   - Every 10 min (between scans, execute mode only): `sync_open_trades()` + `reconcile_orphans()`
   - At each scan interval: `_run_scan()`, increment `scan_count`
   - If `rescan_interval == 0`: scan once then `_wait_for_close()`
3. On close: `_end_of_day()` → `sync_open_trades()` + `ratchet_trailing_stops()`

### `_run_scan(scanner, scan_num)`

1. `sync_open_trades()` (to capture any fills from previous cycle)
2. `check_circuit_breaker()` → if triggered, set `scanner._execute = False` temporarily
3. `scanner.scan()` → candidates
4. `save_scan(candidates, universe_size, regime)`
5. Log result

---

## 16. Persistence Layer

**File:** `data/store.py`  
**Database:** `data/atrades.db` (SQLite, WAL mode)

`init_db()` is idempotent — creates tables if absent and runs `ALTER TABLE` migrations for new columns.

### Table: `scan_runs`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | Auto-increment |
| `ts` | TEXT | UTC timestamp of scan |
| `date` | TEXT | YYYY-MM-DD |
| `symbols_scanned` | INTEGER | Total symbols in universe |
| `candidates_found` | INTEGER | Candidates that qualified |
| `regime` | TEXT | Regime state |
| `adx` | REAL | ADX(14) at scan time |
| `spy_above_200ma` | INTEGER | 1/0 |
| `slope_20d` | REAL | SPY 20d return % |
| `score_multiplier` | REAL | Regime score multiplier |

### Table: `scan_candidates`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | |
| `scan_run_id` | INTEGER FK | Links to `scan_runs.id` |
| `ts / date` | TEXT | Timestamp / date |
| `symbol` | TEXT | Ticker |
| `score` | REAL | Final score |
| `entry / stop / target` | REAL | Trade levels |
| `trail_atr` | REAL | Trailing stop distance |
| `shares / partial_shares / trail_shares` | INTEGER | Share splits |
| `dollar_risk / risk_reward` | REAL | Risk metrics |
| `volume_ratio / rsi / rs_vs_spy` | REAL | Signal metrics |
| `is_trap` | INTEGER | 0/1 |
| `regime` | TEXT | |
| `gap_pct` | REAL | Open vs prior close % |

### Table: `trades`

| Column | Type | Description |
|--------|------|-------------|
| `id` | INTEGER PK | |
| `ts / date` | TEXT | Entry timestamp / date |
| `symbol` | TEXT | |
| `buy_order_id` | TEXT | Alpaca buy order ID |
| `partial_order_id` | TEXT | Alpaca limit sell order ID |
| `trail_order_id` | TEXT | Alpaca trailing stop order ID |
| `stop_order_id` | TEXT | Alpaca hard stop order ID |
| `shares / partial_shares / trail_shares` | INTEGER | |
| `entry` | REAL | Signal-time price |
| `fill_price / fill_ts` | REAL / TEXT | Actual fill |
| `stop_loss / partial_target / trail_atr` | REAL | Exit levels |
| `score` | REAL | Score at entry |
| `status` | TEXT | `open` / `partial_exit` / `closed` |
| `exit_price / exit_ts / exit_reason` | REAL/TEXT | Close details |
| `actual_r` | REAL | `(exit - fill) / (fill - stop)` |
| `hold_days` | INTEGER | Days from open to close |

### Key Read Functions

| Function | Returns |
|----------|---------|
| `query_day(day)` | All unique candidates from all scans that day (best score per symbol), plus trades for that day |
| `query_history(days=30)` | Per-day summary: symbols scanned, unique candidates found, trades placed, regime, ADX, score multiplier |
| `get_open_trades()` | All trades with status `open` or `partial_exit` |
| `query_performance(days=90)` | Aggregate stats for closed trades: win rate, avg R, profit factor, Sharpe-like, best/worst R, by exit reason, last 10 closed |

---

## 17. Web Dashboard

**Server:** `webapp/app.py` (FastAPI)  
**Frontend:** `webapp/static/index.html` (single-page, vanilla JS)

### REST API Endpoints

| Method | Path | Description | Key Response Fields |
|--------|------|-------------|---------------------|
| `GET` | `/` | Serve dashboard HTML | — |
| `GET` | `/api/dashboard?date=` | Day detail: scans, candidates, trades | `scan_count`, `candidates` (all unique), `trades`, `scan_times` |
| `GET` | `/api/history?days=30` | 30-day summary | Per-day: `date`, `candidates_found`, `trades_placed`, `regime`, `adx` |
| `GET` | `/api/positions` | Live Alpaca positions | `market_open`, `positions[]`, `next_open` |
| `GET` | `/api/live-trades` | Merged DB + Alpaca position data | `trades[]` with `unrealized_r`, `stop_dist_pct`, `days_held` |
| `GET` | `/api/performance?days=90` | Closed trade P&L stats | `win_rate`, `avg_r`, `profit_factor`, `best_r`, `worst_r`, `recent` |
| `GET` | `/api/config` | All config values merged with defaults | Full config dict |
| `POST` | `/api/config` | Write selected keys to `.env`, reload config | `{"ok": true}` |
| `GET` | `/api/trading-mode` | Current paper/live mode | `is_paper`, `base_url` |
| `GET` | `/api/runner/status` | Runner daemon status | `running`, `is_paper` |
| `POST` | `/api/runner/start` | Start `runner.py` subprocess | `{"ok": true, "running": true}` |
| `POST` | `/api/runner/stop` | Terminate `runner.py` subprocess | `{"ok": true, "running": false}` |
| `POST` | `/api/scan/start` | Run `scanner.py` subprocess | `{"ok": true}` |
| `GET` | `/api/scan/output?offset=N` | Poll subprocess stdout lines | `lines[]`, `offset`, `running` |
| `GET` | `/api/scan/next` | Next scheduled scan info | `last_scan_ts`, `next_scan_ts`, `interval_minutes`, `runner_running`, `scan_running` |
| `GET` | `/api/symbols/fallback` | Custom override list or S&P 500 defaults | `symbols[]`, `is_custom`, `default_count` |
| `POST` | `/api/symbols/fallback` | Save custom symbol list | `{"ok": true, "count": N}` |
| `DELETE` | `/api/symbols/fallback/reset` | Delete override, restore S&P 500 | `{"ok": true, "count": N}` |

### Dashboard Tabs

#### Today Tab
- **Open Positions** — live data from `/api/live-trades`. Shows fill price, current price, unrealized P&L, unrealized R (multiples of initial risk), stop price, partial target, days held. Summary footer shows total P&L and average R.
- **Trades Placed Today** — from DB, shows all orders placed today.
- **Scanner Candidates** — top 5 from latest scan run, with score bar, entry/stop/target, volume ratio, RSI, RS vs SPY, trap flag.

#### Last 30 Days Tab
- **Equity-like chart** — candidates found per day using Chart.js
- **Daily Summary table** — each row is a trading day. Clicking a row expands inline to show all unique candidates from all scan runs that day (best score per symbol, labelled with scan count). Columns: date, regime pill, ADX, symbols scanned, candidates found, trades placed, score multiplier.

#### Trades / Performance Tab
- **Performance Card** — win rate, avg R, profit factor, best/worst R, recent closed trades. Sources from `/api/performance`.
- **P&L tracking** for closed trades with `actual_r` and `hold_days`.

#### Config Tab

Sections (in order):
1. **Scanner Thresholds** — min score (with weight badge), min price, min volume, RSI bounds (with badges), ATR/consolidation/higher-lows lookbacks, ATR expansion threshold
2. **Signal Weights** — full-width card, 9 base signal inputs + 2 adjustment inputs (accumulation bonus, trap penalty). Live total badge turns green when base weights sum to exactly 100. Changing weights immediately updates the threshold badges.
3. **Risk Management** — max portfolio risk %, max position size %, max open trades, max daily loss %
4. **Trade Setup** — partial exit R, partial exit %, trailing stop ATR multiplier
5. **Bull Trap & Accumulation** — individual check thresholds
6. **Market Regime** — regime-aware toggle, override select
7. **Scanner Schedule** — interval (5/15/30/60 min dropdown), timeframe
8. **Backtest Settings** — max hold days, slippage, initial capital
9. **Trading Mode** — paper/live toggle, base URL
10. **Scanner Universe** — chip display of all symbols, filter by search, add new symbol, remove individual, reset to S&P 500
11. **Alpaca Credentials** — masked inputs for API key and secret

**Save flow:** `POST /api/config` (all non-empty fields from CONFIG_DEFAULTS) + `POST /api/symbols/fallback` (symbols list) in parallel. Config reloads immediately in the running process.

### Header Controls

- **Regime badge** — live colour-coded regime indicator (green/yellow/red)
- **Auto-refresh countdown** — ticks every second, triggers full data reload every 15 minutes
- **Next scan countdown** — appears only when runner is active. Shows `Next scan M:SS`, turns cyan when < 60 seconds away. Syncs with `/api/scan/next` every 30 seconds.
- **Date picker** — navigate to any past date
- **Scan Now button** — triggers `POST /api/scan/start`. Has DRY RUN / LIVE toggle. When runner is active, shows Stop Runner instead.
- **Scan output panel** — slide-in drawer showing live scanner subprocess stdout, colour-coded (green = signals, yellow = warnings, red = errors). Shows summary on completion with Scan Again button.
- **Live trading banner** — red warning bar shown when `IS_PAPER=false`

---

## 18. Backtest Engine

**File:** `backtest/engine.py`  
**Class:** `BacktestEngine`

### `run(market_data, spy_data, start_date, end_date, max_concurrent, min_score) → BacktestResults`

Event-driven simulation. Signal fires on day T close; entry simulated at day T+1 open + slippage.

**Per-day loop:**
1. Evaluate all open trades on `entry_date` (Phase 1 or 2 exit logic)
2. Compute open slots: `max_concurrent - len(open_trades)`
3. Scan `signal_date` for new setups using `detect_all()` + `score()`
4. Sort candidates by score, take top `slots`
5. For each: re-compute `calculate_setup()` at actual T+1 open price, create `Trade`

**Trade lifecycle:**

*Phase 1 (before partial exit):*
- `low <= stop_loss` → exit at stop (loss)
- `high >= target_price` → partial exit; if `trail_shares > 0`, start Phase 2
- `days_held >= BACKTEST_MAX_HOLD_DAYS` → timeout at close

*Phase 2 (after partial exit):*
- Each day: ratchet `trail_stop = max(trail_stop, close - trail_atr, prev_candle_low)`
- `low <= trail_stop` → exit at trail stop (win if total P&L > 0, else loss)
- `days_held >= BACKTEST_MAX_HOLD_DAYS` → timeout

**Remaining open trades** at end of test: closed at last available close.

### BacktestResults Properties

| Property | Calculation |
|----------|-------------|
| `win_rate` | wins / closed_trades × 100 |
| `total_return_pct` | (final / initial - 1) × 100 |
| `profit_factor` | gross_profit / gross_loss |
| `max_drawdown_pct` | min rolling drawdown from equity curve |
| `sharpe_ratio` | mean(daily_returns) / std × √252 |

---

## 19. Parameter Optimizer

**File:** `backtest/optimizer.py`  
**Class:** `ParameterOptimizer`

Grid search over `DEFAULT_GRID` (81 combinations = 3⁴):

| Parameter | Values Tested |
|-----------|--------------|
| `BREAKOUT_RSI_LOW` | 50, 55, 60 |
| `BREAKOUT_VOLUME_SURGE_MULT` | 1.3, 1.5, 2.0 |
| `BREAKOUT_ATR_EXPANSION_THRESHOLD` | 1.0, 1.2, 1.5 |
| `BACKTEST_MAX_HOLD_DAYS` | 10, 15, 20 |

**Two-phase design for speed:**

**Phase 1 — Precompute** (once per run): For each symbol, vectorise all signal metrics as pandas Series aligned to a common DatetimeIndex: `breakout_20d`, `breakout_50d`, `vol_ratio`, `rsi`, `atr_exp`, `rs_vs_spy`, `base_ok`, `atr14`. `O(symbols × dates)`.

**Phase 2 — Evaluate** (per combo): Apply threshold masks to precomputed Series (pure boolean operations, no Python loops over rows). Run trade simulation only for days where the mask is `True`. `O(combos × signal_days)` ≈ 50–200× faster than full `detect_all()` inside each combo loop.

**Scoring uses `_fast_score()`** — a lightweight 0–100 approximation from precomputed Series, skipping full accumulation/trap detection.

Results ranked by chosen metric (`sharpe`, `win_rate`, `profit_factor`, `total_return`). Best params saved to `best_params.env` for manual review and merge into `.env`.

---

## 20. Broker Client

**File:** `broker/alpaca_client.py`  
**Class:** `AlpacaClient`

Thin wrapper around `alpaca-trade-api-python`. Initialises `TradingClient` from `config.ALPACA_API_KEY`, `config.ALPACA_SECRET_KEY`, `config.IS_PAPER`.

Key methods:
- `get_portfolio_value() → float` — equity from account info
- `get_cash() → float`
- `get_position(symbol) → Position | None`
- `get_all_positions() → list[Position]`
- `is_market_open() → bool`
- `.trading_client` — direct access to the Alpaca `TradingClient` for order submission

---

## 21. End-to-End Flow

### Automated Daily Trading (runner.py)

```
[Before Market Open]
  runner.py starts → _wait_for_open() polls Alpaca clock
  When open: sleep 5 min (price settling)

[Trading Session]
  Every scan interval (default 60 min):
    sync_open_trades()           → reconcile DB with Alpaca fills
    check_circuit_breaker()      → if down 4%+ today, disable execution
    scanner.scan()               → full 501-symbol pipeline
      For each symbol:
        detect_all() → 3-way gate → signals
        score() → regime multiply → threshold filter
        calculate_setup() → stop/target/sizing
      Top 5 sorted by score
    If execute:
      _place_orders() up to (MAX_CONCURRENT - open_positions) slots
        BracketOrderExecutor.place():
          Market buy → poll fill → hard stop → limit sell
    save_scan() → SQLite

  Every 10 min between scans (execute mode):
    sync_open_trades()           → check partial fills, upgrade to trail
    reconcile_orphans()          → plug any naked positions

[Market Close]
  sync_open_trades()
  ratchet_trailing_stops()       → tighten trails based on new ATR
```

### Manual Scan via Web Dashboard

```
User clicks "Scan Now" (DRY RUN mode)
  → POST /api/scan/start
  → Server spawns scanner.py subprocess
  → Client polls GET /api/scan/output?offset=N every 1 second
  → Lines streamed to slide-in panel (colour-coded)
  → On "running: false": show summary, enable "Scan Again"
  → Dashboard auto-reloads data (loadToday() called)
```

---

## 22. Risk Controls Summary

| Control | Mechanism | Threshold |
|---------|-----------|-----------|
| Score floor | `score >= effective_min_score` | 55–70 (regime-dependent) |
| Regime gate | Score multiplier + min score override | ×0.40–1.00 |
| Position size limit | `shares_by_size = position_cap / entry` | 10% of portfolio |
| Dollar risk per trade | `shares_by_risk = risk_budget / risk_per_share` | 1% of portfolio |
| Concurrent trade limit | `slots = MAX_CONCURRENT - open_positions` | 5 (configurable) |
| Hard stop | GTC stop order at entry − 2×ATR (floor: 80% of entry) | 2×ATR14 |
| Trap filter | `is_trap` check before order submission | trap_score ≥ 40 |
| Circuit breaker | Daily portfolio loss vs start-of-day value | 4% daily loss |
| Orphan protection | `reconcile_orphans()` every 10 min | 5% emergency stop |
| Trailing stop ratchet | ATR-based tightening post-partial-exit | 2×ATR, only tightens |
| Max stop floor | `stop >= entry × 0.80` | 20% max stop distance |
| Target cap | `target <= entry × 1.50` | 1.5× entry |
