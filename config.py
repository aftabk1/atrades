import os
from pathlib import Path
from dotenv import load_dotenv

# Pin to this file's directory so .env is always found regardless of CWD
_ENV_PATH = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_ENV_PATH, override=True)

# ── Alpaca credentials ────────────────────────────────────────────────────────
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
IS_PAPER = os.getenv("IS_PAPER", "true").lower() == "true"

# ── Main trading loop ─────────────────────────────────────────────────────────
SYMBOLS = [s.strip() for s in os.getenv("SYMBOLS", "AAPL,MSFT,GOOGL").split(",")]

# ── Portfolio risk limits ─────────────────────────────────────────────────────
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "0.10"))   # fraction of portfolio per trade
MAX_PORTFOLIO_RISK = float(os.getenv("MAX_PORTFOLIO_RISK", "0.01")) # max loss per trade as fraction
MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "4"))

# ── Breakout scanner — base filters ──────────────────────────────────────────
BREAKOUT_MIN_PRICE = float(os.getenv("BREAKOUT_MIN_PRICE", "25.0"))
BREAKOUT_MIN_AVG_VOLUME = int(os.getenv("BREAKOUT_MIN_AVG_VOLUME", "1000000"))

# ── Breakout scanner — signal thresholds ─────────────────────────────────────
BREAKOUT_VOLUME_SURGE_MULT = float(os.getenv("BREAKOUT_VOLUME_SURGE_MULT", "1.5"))
BREAKOUT_RSI_LOW = float(os.getenv("BREAKOUT_RSI_LOW", "50.0"))   # was 55 — catch momentum earlier
BREAKOUT_RSI_HIGH = float(os.getenv("BREAKOUT_RSI_HIGH", "65.0"))  # was 70 — exclude extended stocks
BREAKOUT_CONSOLIDATION_DAILY_VOL = float(os.getenv("BREAKOUT_CONSOLIDATION_DAILY_VOL", "0.015"))
BREAKOUT_CONSOLIDATION_LOOKBACK = int(os.getenv("BREAKOUT_CONSOLIDATION_LOOKBACK", "15"))
BREAKOUT_HIGHER_LOWS_LOOKBACK = int(os.getenv("BREAKOUT_HIGHER_LOWS_LOOKBACK", "15"))

# ── Breakout scanner — freshness / extension guards ──────────────────────────
BREAKOUT_MAX_AGE_DAYS      = int(os.getenv("BREAKOUT_MAX_AGE_DAYS",      "3"))
BREAKOUT_MAX_EXTENSION_PCT = float(os.getenv("BREAKOUT_MAX_EXTENSION_PCT", "0.08"))

# ── Breakout scanner — trade setup ────────────────────────────────────────────
BREAKOUT_ATR_STOP_MULT    = float(os.getenv("BREAKOUT_ATR_STOP_MULT",    "2.0"))
BREAKOUT_MAX_STOP_PCT     = float(os.getenv("BREAKOUT_MAX_STOP_PCT",     "0.20")) # stop floor: never below 80% of entry
BREAKOUT_SUPPORT_LOOKBACK = int(os.getenv("BREAKOUT_SUPPORT_LOOKBACK",   "10"))   # days for swing-low support
BREAKOUT_RR_RATIO         = float(os.getenv("BREAKOUT_RR_RATIO",         "2.0"))  # partial exit at 2R
BREAKOUT_MIN_SCORE        = float(os.getenv("BREAKOUT_MIN_SCORE",        "65.0")) # 0–100 score floor

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

# ── Signal score weights ──────────────────────────────────────────────────────
# Base 9 signals sum to 94; market breadth adds up to 6 → effective max 100.
# Breakdown: predictive (VCP+consolidation+higher_lows+52w+earnings) = 54 pts
#            confirmation (volume+breakout_20d+rsi+rs)               = 40 pts
#            breadth (market participation)                           =  6 pts
#
# Removed: breakout_50d (replaced by high_52w_proximity)
#          atr_expansion (replaced by vcp)
#          regime score multiplier (replaced by market_breadth)

# Predictive / setup-quality
SCORE_VCP                = float(os.getenv("SCORE_VCP",                "14.0"))
SCORE_CONSOLIDATION      = float(os.getenv("SCORE_CONSOLIDATION",      "12.0"))
SCORE_HIGHER_LOWS        = float(os.getenv("SCORE_HIGHER_LOWS",        "12.0"))
SCORE_52W_HIGH_PROXIMITY = float(os.getenv("SCORE_52W_HIGH_PROXIMITY", "10.0"))
SCORE_EARNINGS_PROXIMITY = float(os.getenv("SCORE_EARNINGS_PROXIMITY",  "6.0"))

# Confirmation
SCORE_VOLUME_SURGE       = float(os.getenv("SCORE_VOLUME_SURGE",       "14.0"))
SCORE_BREAKOUT_20D       = float(os.getenv("SCORE_BREAKOUT_20D",       "12.0"))
SCORE_RSI_ZONE           = float(os.getenv("SCORE_RSI_ZONE",           "10.0"))
SCORE_RELATIVE_STRENGTH  = float(os.getenv("SCORE_RELATIVE_STRENGTH",   "4.0"))

# Market breadth (computed at scan level, not per-symbol)
SCORE_MARKET_BREADTH     = float(os.getenv("SCORE_MARKET_BREADTH",      "6.0"))

SCORE_ACCUM_MAX_BONUS    = float(os.getenv("SCORE_ACCUM_MAX_BONUS",    "12.0"))
SCORE_TRAP_MAX_PENALTY   = float(os.getenv("SCORE_TRAP_MAX_PENALTY",   "32.0"))

# ── Runner schedule ───────────────────────────────────────────────────────────
SCANNER_INTERVAL_MINUTES = int(os.getenv("SCANNER_INTERVAL_MINUTES", "60"))

# ── Market regime ─────────────────────────────────────────────────────────────
REGIME_AWARE_SCANNING = os.getenv("REGIME_AWARE_SCANNING", "true").lower() == "true"
REGIME_OVERRIDE       = os.getenv("REGIME_OVERRIDE", "")   # blank = auto-detect; or BULL_TREND/SIDEWAYS/BEAR_TREND/HIGH_VOLATILITY

# ── Backtest ──────────────────────────────────────────────────────────────────
BACKTEST_MAX_HOLD_DAYS = int(os.getenv("BACKTEST_MAX_HOLD_DAYS", "20"))
BACKTEST_SLIPPAGE_PCT = float(os.getenv("BACKTEST_SLIPPAGE_PCT", "0.0005")) # 0.05% slippage
BACKTEST_INITIAL_CAPITAL = float(os.getenv("BACKTEST_INITIAL_CAPITAL", "100000"))

# ── Notifications ─────────────────────────────────────────────────────────────
WHATSAPP_PHONE  = os.getenv("WHATSAPP_PHONE",  "")   # international format, e.g. +97455512345
WHATSAPP_APIKEY = os.getenv("WHATSAPP_APIKEY", "")   # from CallMeBot setup

# ── Position Management Engine ────────────────────────────────────────────────
PME_ADD_SCORE_THRESHOLD    = int(os.getenv("PME_ADD_SCORE_THRESHOLD",    "75"))
PME_HOLD_SCORE_MIN         = int(os.getenv("PME_HOLD_SCORE_MIN",         "60"))
PME_TRIM_LIGHT_SCORE_MIN   = int(os.getenv("PME_TRIM_LIGHT_SCORE_MIN",   "55"))
PME_TRIM_HEAVY_SCORE_MIN   = int(os.getenv("PME_TRIM_HEAVY_SCORE_MIN",   "45"))
PME_ADD_SIZE_PCT           = float(os.getenv("PME_ADD_SIZE_PCT",           "0.20"))
PME_ADD_MAX_MULTIPLIER     = float(os.getenv("PME_ADD_MAX_MULTIPLIER",     "1.50"))
PME_TRIM_LIGHT_PCT         = float(os.getenv("PME_TRIM_LIGHT_PCT",         "0.25"))
PME_TRIM_HEAVY_PCT         = float(os.getenv("PME_TRIM_HEAVY_PCT",         "0.60"))
PME_RS_ADD_MIN_PCT         = float(os.getenv("PME_RS_ADD_MIN_PCT",         "8.0"))
PME_RS_DOWNGRADE_BELOW_PCT = float(os.getenv("PME_RS_DOWNGRADE_BELOW_PCT", "2.0"))
PME_R_TRIM_FLOOR           = float(os.getenv("PME_R_TRIM_FLOOR",           "2.0"))
PME_R_TRIM_ENFORCE         = float(os.getenv("PME_R_TRIM_ENFORCE",         "3.0"))
PME_FOLLOWTHROUGH_DAYS     = int(os.getenv("PME_FOLLOWTHROUGH_DAYS",      "2"))
PME_VOLUME_SELLOFF_MULT    = float(os.getenv("PME_VOLUME_SELLOFF_MULT",    "2.0"))
