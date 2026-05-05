import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ── Alpaca credentials ────────────────────────────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
IS_PAPER = os.getenv("IS_PAPER", "true").lower() == "true"

# ── Main trading loop ─────────────────────────────────────────────────────────
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "AAPL,MSFT,GOOGL").split(",")]
TIMEFRAME = os.getenv("TIMEFRAME", "1Min")

# ── Portfolio risk limits ─────────────────────────────────────────────────────
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "0.10"))   # fraction of portfolio per trade
MAX_PORTFOLIO_RISK = float(os.getenv("MAX_PORTFOLIO_RISK", "0.01")) # max loss per trade as fraction
MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "4"))

# ── Breakout scanner — base filters ──────────────────────────────────────────
BREAKOUT_MIN_PRICE = float(os.getenv("BREAKOUT_MIN_PRICE", "25.0"))
BREAKOUT_MIN_AVG_VOLUME = int(os.getenv("BREAKOUT_MIN_AVG_VOLUME", "1000000"))

# ── Breakout scanner — signal thresholds ─────────────────────────────────────
BREAKOUT_VOLUME_SURGE_MULT = float(os.getenv("BREAKOUT_VOLUME_SURGE_MULT", "1.5"))
BREAKOUT_RSI_LOW = float(os.getenv("BREAKOUT_RSI_LOW", "55.0"))
BREAKOUT_RSI_HIGH = float(os.getenv("BREAKOUT_RSI_HIGH", "70.0"))
BREAKOUT_ATR_EXPANSION_THRESHOLD = float(os.getenv("BREAKOUT_ATR_EXPANSION_THRESHOLD", "1.2"))
BREAKOUT_CONSOLIDATION_DAILY_VOL = float(os.getenv("BREAKOUT_CONSOLIDATION_DAILY_VOL", "0.015"))
BREAKOUT_CONSOLIDATION_LOOKBACK = int(os.getenv("BREAKOUT_CONSOLIDATION_LOOKBACK", "15"))
BREAKOUT_HIGHER_LOWS_LOOKBACK = int(os.getenv("BREAKOUT_HIGHER_LOWS_LOOKBACK", "15"))

# ── Breakout scanner — trade setup ────────────────────────────────────────────
BREAKOUT_ATR_STOP_MULT    = float(os.getenv("BREAKOUT_ATR_STOP_MULT",    "2.0"))
BREAKOUT_MAX_STOP_PCT     = float(os.getenv("BREAKOUT_MAX_STOP_PCT",     "0.20")) # stop floor: never below 80% of entry
BREAKOUT_SUPPORT_LOOKBACK = int(os.getenv("BREAKOUT_SUPPORT_LOOKBACK",   "10"))   # days for swing-low support
BREAKOUT_RR_RATIO         = float(os.getenv("BREAKOUT_RR_RATIO",         "2.0"))  # partial exit at 2R
BREAKOUT_MIN_SCORE        = float(os.getenv("BREAKOUT_MIN_SCORE",        "60.0")) # 0–100 score floor

# ── Partial exit + trailing stop ──────────────────────────────────────────────
PARTIAL_EXIT_R   = float(os.getenv("PARTIAL_EXIT_R",   "2.0"))   # take partial profit at this R multiple
PARTIAL_EXIT_PCT = float(os.getenv("PARTIAL_EXIT_PCT", "0.50"))  # fraction of position to exit at partial
TRAIL_ATR_MULT   = float(os.getenv("TRAIL_ATR_MULT",   "2.0"))   # trailing stop distance = N × ATR14

# ── Breakout scanner — accumulation detection ─────────────────────────────────
ACCUM_LOOKBACK_DAYS = int(os.getenv("ACCUM_LOOKBACK_DAYS", "20"))

# ── Breakout scanner — bull trap detection ────────────────────────────────────
BULL_TRAP_SCORE_THRESHOLD = float(os.getenv("BULL_TRAP_SCORE_THRESHOLD", "40.0"))

# ── Risk circuit breaker ──────────────────────────────────────────────────────
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.04"))  # halt if down 4% on the day

# ── Gap-up detection ──────────────────────────────────────────────────────────
GAP_UP_THRESHOLD = float(os.getenv("GAP_UP_THRESHOLD", "0.08"))  # ≥8% open vs prior close = gap-up

# ── Signal score weights (must sum to 100 for a clean score scale) ────────────
SCORE_VOLUME_SURGE       = float(os.getenv("SCORE_VOLUME_SURGE",       "20.0"))
SCORE_BREAKOUT_20D       = float(os.getenv("SCORE_BREAKOUT_20D",       "16.0"))
SCORE_RELATIVE_STRENGTH  = float(os.getenv("SCORE_RELATIVE_STRENGTH",  "12.0"))
SCORE_RSI_ZONE           = float(os.getenv("SCORE_RSI_ZONE",           "12.0"))
SCORE_BREAKOUT_50D       = float(os.getenv("SCORE_BREAKOUT_50D",       "12.0"))
SCORE_ATR_EXPANSION      = float(os.getenv("SCORE_ATR_EXPANSION",       "8.0"))
SCORE_CONSOLIDATION      = float(os.getenv("SCORE_CONSOLIDATION",       "8.0"))
SCORE_HIGHER_LOWS        = float(os.getenv("SCORE_HIGHER_LOWS",         "8.0"))
SCORE_EARNINGS_PROXIMITY = float(os.getenv("SCORE_EARNINGS_PROXIMITY",  "4.0"))
SCORE_ACCUM_MAX_BONUS    = float(os.getenv("SCORE_ACCUM_MAX_BONUS",    "12.0"))
SCORE_TRAP_MAX_PENALTY   = float(os.getenv("SCORE_TRAP_MAX_PENALTY",   "32.0"))

# ── Market regime ─────────────────────────────────────────────────────────────
REGIME_AWARE_SCANNING = os.getenv("REGIME_AWARE_SCANNING", "true").lower() == "true"

# ── Backtest ──────────────────────────────────────────────────────────────────
BACKTEST_MAX_HOLD_DAYS = int(os.getenv("BACKTEST_MAX_HOLD_DAYS", "20"))
BACKTEST_SLIPPAGE_PCT = float(os.getenv("BACKTEST_SLIPPAGE_PCT", "0.0005")) # 0.05% slippage
BACKTEST_INITIAL_CAPITAL = float(os.getenv("BACKTEST_INITIAL_CAPITAL", "100000"))
