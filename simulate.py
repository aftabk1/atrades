"""
Trade simulation — walks forward day-by-day from an entry and shows exactly
how the stop / partial-exit / trailing-stop logic plays out.

Usage:
  python simulate.py                        # simulate top scanner candidate
  python simulate.py --symbol AMD           # specific symbol
  python simulate.py --symbol MU --entry 2026-04-10   # custom entry date
  python simulate.py --symbol SNDK --days 30          # look back further for entry
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

import config

# ── Config ────────────────────────────────────────────────────────────────────
ATR_STOP_MULT  = config.BREAKOUT_ATR_STOP_MULT   # 2.0
PARTIAL_R      = config.PARTIAL_EXIT_R            # 2.0
PARTIAL_PCT    = config.PARTIAL_EXIT_PCT          # 0.50
TRAIL_ATR_MULT = config.TRAIL_ATR_MULT            # 2.0
PORTFOLIO      = 100_000.0
RISK_PCT       = config.MAX_PORTFOLIO_RISK        # 0.02
MAX_POSITION   = config.MAX_POSITION_SIZE         # 0.10


# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch(symbol: str, days: int = 180) -> pd.DataFrame:
    start = (datetime.now() - timedelta(days=days + 10)).strftime("%Y-%m-%d")
    df = yf.download(symbol, start=start, auto_adjust=True, progress=False)
    # yfinance may return MultiIndex columns — flatten to single level
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0].lower() for c in df.columns]
    else:
        df.columns = [c.lower() for c in df.columns]
    df.index = pd.to_datetime(df.index).normalize()
    return df.sort_index()


def atr14(df: pd.DataFrame) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=14, adjust=False).mean()


def swing_low(df: pd.DataFrame, lookback: int = config.BREAKOUT_SUPPORT_LOOKBACK) -> float:
    lows = df["low"].tail(lookback).values
    swing_lows = [lows[i] for i in range(1, len(lows) - 1)
                  if lows[i] <= lows[i-1] and lows[i] <= lows[i+1]]
    return float(min(swing_lows)) if swing_lows else float(lows.min())


def setup_at(df: pd.DataFrame, entry_date: pd.Timestamp) -> dict | None:
    hist = df[df.index <= entry_date]
    if len(hist) < 30:
        return None

    entry   = float(hist.iloc[-1]["close"])
    atr_ser = atr14(hist)
    atr     = float(atr_ser.iloc[-1])
    sup     = swing_low(hist) * 0.995

    atr_stop    = entry - ATR_STOP_MULT * atr
    stop_floor  = entry * (1.0 - config.BREAKOUT_MAX_STOP_PCT)  # 80% of entry
    stop        = max(min(atr_stop, sup), stop_floor)
    if stop >= entry:
        return None

    risk_ps  = entry - stop
    target   = min(entry + PARTIAL_R * risk_ps, entry * 1.5)
    trail    = TRAIL_ATR_MULT * atr

    shares_r = int((PORTFOLIO * RISK_PCT) / risk_ps)
    shares_s = int((PORTFOLIO * MAX_POSITION) / entry)
    shares   = max(min(shares_r, shares_s), 1)

    partial = max(int(shares * PARTIAL_PCT), 1) if shares >= 2 else shares
    trail_s = shares - partial

    return {
        "entry":        round(entry, 2),
        "stop":         round(stop,  2),
        "atr_stop":     round(atr_stop, 2),
        "sup_stop":     round(sup, 2),
        "target":       round(target, 2),
        "trail_atr":    round(trail, 2),
        "atr":          round(atr, 2),
        "risk_ps":      round(risk_ps, 2),
        "shares":       shares,
        "partial":      partial,
        "trail_sh":     trail_s,
        "dollar_risk":  round(shares * risk_ps, 2),
    }


# ── Simulation ────────────────────────────────────────────────────────────────

def simulate(symbol: str, entry_date: str | None, days: int) -> None:
    print(f"\n{'=' * 62}")
    print(f"  TRADE SIMULATION  --  {symbol}")
    print(f"{'=' * 62}")

    df = fetch(symbol, days=max(days, 180))
    if df.empty:
        print(f"  No data for {symbol}")
        return

    # Pick entry date
    if entry_date:
        edate = pd.Timestamp(entry_date)
    else:
        # Use most recent session
        edate = df.index[-1]

    if edate not in df.index:
        # Find nearest available date
        edate = df.index[df.index.get_indexer([edate], method="ffill")[0]]

    setup = setup_at(df, edate)
    if setup is None:
        print(f"  Cannot build setup at {edate.date()} — insufficient history or ATR=0")
        return

    print(f"\n  Entry date   : {edate.date()}")
    print(f"  Entry price  : ${setup['entry']:.2f}")
    floor = round(setup["entry"] * (1.0 - config.BREAKOUT_MAX_STOP_PCT), 2)
    print(f"\n  -- Stop calculation --")
    print(f"     ATR(14)         = ${setup['atr']:.2f}")
    print(f"     ATR stop        = ${setup['entry']:.2f} - 2x${setup['atr']:.2f}        = ${setup['atr_stop']:.2f}")
    print(f"     Support stop    = 10d swing_low x 0.995           = ${setup['sup_stop']:.2f}")
    print(f"     Floor           = ${setup['entry']:.2f} x 80%                = ${floor:.2f}")
    print(f"     STOP = MAX(MIN(ATR, Support), Floor)              = ${setup['stop']:.2f}")
    print(f"     Risk/share      = ${setup['entry']:.2f} - ${setup['stop']:.2f}          = ${setup['risk_ps']:.2f}")
    print(f"\n  -- Exit plan --")
    print(f"     Partial target  = ${setup['entry']:.2f} + 2R (${setup['risk_ps']:.2f} x 2) = ${setup['target']:.2f}")
    print(f"     Trail distance  = 2 x ATR = ${setup['trail_atr']:.2f}")
    print(f"\n  -- Position sizing --")
    print(f"     Total shares    : {setup['shares']} sh  (${setup['shares']*setup['entry']:,.0f} notional)")
    print(f"     Partial (50%)   : {setup['partial']} sh  -> exit at ${setup['target']:.2f}")
    print(f"     Trail (50%)     : {setup['trail_sh']} sh -> trail with -${setup['trail_atr']:.2f}")
    print(f"     Dollar risk     : ${setup['dollar_risk']:,.0f}  ({setup['dollar_risk']/PORTFOLIO:.1%} of portfolio)")

    # Walk forward
    forward = df[df.index > edate].head(30)
    if forward.empty:
        print("\n  No forward data to simulate (entry is on the latest available date).")
        print("  Run again after tomorrow's close to see results.")
        _print_tomorrow_guide(symbol, setup)
        return

    print(f"\n  -- Day-by-day simulation ({len(forward)} trading days) --")
    print(f"  {'Day':<4}  {'Date':<12}  {'Open':>7}  {'High':>7}  {'Low':>7}  {'Close':>7}  {'Event'}")
    print(f"  {'-'*4}  {'-'*12}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}  {'-'*30}")

    partial_filled = False
    trail_stop     = setup["stop"]
    pnl            = 0.0
    final_outcome  = "open"
    prev_low       = float(df.loc[edate, "low"]) if edate in df.index else 0.0

    for n, (date, row) in enumerate(forward.iterrows(), 1):
        o  = float(row["open"])
        h  = float(row["high"])
        l  = float(row["low"])
        c  = float(row["close"])
        ev = ""

        if not partial_filled:
            if l <= setup["stop"]:
                pnl = (setup["stop"] - setup["entry"]) * setup["shares"]
                ev  = f"[STOP] All {setup['shares']} sh @ ${setup['stop']:.2f}  PnL ${pnl:+,.0f}"
                final_outcome = "LOSS"
                _print_row(n, date, o, h, l, c, ev)
                break
            elif h >= setup["target"]:
                partial_pnl  = (setup["target"] - setup["entry"]) * setup["partial"]
                pnl         += partial_pnl
                partial_filled = True
                # initialise trail stop
                atr_trail   = c - setup["trail_atr"]
                trail_stop  = max(setup["stop"], atr_trail, prev_low)
                ev  = (
                    f"[PARTIAL] {setup['partial']} sh @ ${setup['target']:.2f}  "
                    f"PnL +${partial_pnl:,.0f}  |  trail_stop init ${trail_stop:.2f}"
                )
                if setup["trail_sh"] < 1:
                    final_outcome = "WIN (full exit)"
                    _print_row(n, date, o, h, l, c, ev)
                    break
            else:
                ev = f"holding  (stop ${setup['stop']:.2f}  target ${setup['target']:.2f})"

        else:
            # ratchet trail stop up
            atr_trail   = c - setup["trail_atr"]
            new_trail   = max(trail_stop, atr_trail, prev_low)
            moved       = new_trail > trail_stop
            trail_stop  = new_trail

            if l <= trail_stop:
                trail_pnl     = (trail_stop - setup["entry"]) * setup["trail_sh"]
                pnl          += trail_pnl
                ev = (
                    f"[TRAIL STOP] {setup['trail_sh']} sh @ ${trail_stop:.2f}  "
                    f"trail PnL {'+' if trail_pnl >= 0 else ''}${trail_pnl:,.0f}  |  "
                    f"Total PnL ${pnl:+,.0f}"
                )
                final_outcome = "WIN" if pnl > 0 else "LOSS"
                _print_row(n, date, o, h, l, c, ev)
                break
            else:
                mv = f"  trail_stop -> ${trail_stop:.2f}" if moved else ""
                ev = f"trailing  (trail_stop ${trail_stop:.2f}){mv}"

        _print_row(n, date, o, h, l, c, ev)
        prev_low = l

    else:
        # 30-day timeout
        last_close = float(forward.iloc[-1]["close"])
        if partial_filled:
            trail_pnl = (last_close - setup["entry"]) * setup["trail_sh"]
            pnl      += trail_pnl
        else:
            pnl = (last_close - setup["entry"]) * setup["shares"]
        final_outcome = "TIMEOUT"
        print(f"\n  Timeout after 30 days. Exit @ close ${last_close:.2f}")

    # Summary
    print(f"\n  {'=' * 58}")
    print(f"  OUTCOME  : {final_outcome}")
    print(f"  TOTAL PnL: ${pnl:+,.2f}")
    if setup["dollar_risk"] > 0:
        print(f"  R multiple: {pnl / setup['dollar_risk']:+.2f}R")
    print(f"  {'=' * 58}")

    _print_tomorrow_guide(symbol, setup)


def _print_row(n, date, o, h, l, c, ev):
    print(f"  {n:<4}  {str(date.date()):<12}  {o:>7.2f}  {h:>7.2f}  {l:>7.2f}  {c:>7.2f}  {ev}")


def _print_tomorrow_guide(symbol: str, setup: dict) -> None:
    print(f"""
{'=' * 62}
  HOW TO TEST TOMORROW AT MARKET OPEN
{'=' * 62}

  1. PRE-MARKET (9:00-9:25 AM ET)
     Run the scanner to get fresh setups:
       python scanner.py
     or for specific symbols:
       python scanner.py --symbols {symbol}

  2. AT MARKET OPEN (9:30 AM ET)
     Dry run (no orders, just print):
       python scanner.py --symbols {symbol}

     Execute on paper account:
       python scanner.py --symbols {symbol} --execute

  3. WHAT GETS PLACED (for {symbol} based on today's setup)
     [BUY]          Market buy {setup['shares']} shares @ ~${setup['entry']:.2f}
     [LIMIT SELL]   {setup['partial']} shares @ ${setup['target']:.2f}  (partial 2R)
     [TRAIL STOP]   {setup['trail_sh']} shares, trail = ${setup['trail_atr']:.2f}

  4. WHAT TO WATCH
     Stop loss level : ${setup['stop']:.2f}  <- exit ALL if price drops here
     Partial target  : ${setup['target']:.2f}  <- 50% exits here automatically
     Trail kicks in  : after partial fills, trailing stop starts at
                       MAX(stop, close - ${setup['trail_atr']:.2f}, prev candle low)
                       and ratchets up each candle

  5. VERIFY ORDERS IN ALPACA PAPER DASHBOARD
     https://app.alpaca.markets/paper/dashboard/overview
     Check: 1 market buy + 1 limit sell + 1 trailing stop for {symbol}

  6. INTRADAY MONITORING (optional — re-scans every 5 min)
       python scanner.py --intraday
""")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(description="Simulate partial-exit + trailing-stop trade")
    p.add_argument("--symbol",  default=None, help="Symbol to simulate (default: AMD)")
    p.add_argument("--entry",   default=None, help="Entry date YYYY-MM-DD (default: latest)")
    p.add_argument("--days",    type=int, default=180, help="Days of history to fetch")
    args = p.parse_args()

    symbol = (args.symbol or "AMD").upper()
    simulate(symbol, args.entry, args.days)


if __name__ == "__main__":
    main()
