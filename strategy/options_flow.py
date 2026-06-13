"""
Options flow signal detector.

Three sub-signals derived from the yfinance options chain:

  1. Put/Call ratio (volume) — call buying dominance signals bullish positioning.
     PCR < 0.7 across the nearest two expiries = smart money buying calls.

  2. Call/Put open-interest ratio (ATM ±5%) — OI skew shows where bets are
     concentrated. Ratio > 1.5 = more bullish open positions near current price.

  3. IV Rank — compares today's ATM implied volatility to the trailing 52-week
     HV range. IV Rank < 30 means options are historically cheap, implying the
     market has NOT priced in a big move — ideal for breakout entries because the
     breakout is still a surprise.

Composite score 0–1 → scorer converts to max +10 pts bonus.

Fetched per-symbol in scanner Phase 2 (only for pre-qualifiers) to keep
scan time manageable. Falls back to None gracefully on any error.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import yfinance as yf


@dataclass
class OptionsFlowResult:
    symbol: str
    pcr: float                  # put/call volume ratio  (<0.7 = bullish)
    atm_oi_ratio: float         # call/put OI near ATM   (>1.5 = bullish)
    iv_rank: float              # 0–100; <30 = cheap options
    atm_iv: float               # ATM implied vol %
    unusual_call_vol: bool      # call vol > put vol by 2× or more
    composite_score: float      # 0–1 used by scorer
    description: str


def detect_options_flow(symbol: str) -> OptionsFlowResult | None:
    """
    Fetch the options chain for `symbol` and compute the three sub-signals.
    Returns None if options data is unavailable or the symbol has no listed options.
    Uses the two nearest expiry dates for best liquidity.
    """
    try:
        ticker = yf.Ticker(symbol)
        expiries = ticker.options
        if not expiries:
            return None

        # ── Aggregate across nearest 2 expiries ───────────────────────────────
        total_call_vol = 0.0
        total_put_vol  = 0.0
        atm_iv_samples: list[float] = []
        atm_call_oi    = 0.0
        atm_put_oi     = 0.0

        price = float(ticker.fast_info.get("last_price") or ticker.fast_info.get("lastPrice") or 0)
        if price <= 0:
            return None

        for expiry in expiries[:2]:
            chain = ticker.option_chain(expiry)
            calls = chain.calls
            puts  = chain.puts

            total_call_vol += float(calls["volume"].fillna(0).sum())
            total_put_vol  += float(puts["volume"].fillna(0).sum())

            # ATM = strike closest to current price
            if not calls.empty:
                atm_idx = int((calls["strike"] - price).abs().idxmin())
                iv_val  = float(calls.loc[atm_idx, "impliedVolatility"])
                if np.isfinite(iv_val) and iv_val > 0:
                    atm_iv_samples.append(iv_val * 100)

            # OI skew within ±5% of current price
            atm_calls = calls[(calls["strike"] >= price * 0.95) &
                               (calls["strike"] <= price * 1.05)]
            atm_puts  = puts[(puts["strike"]  >= price * 0.95) &
                              (puts["strike"]  <= price * 1.05)]
            atm_call_oi += float(atm_calls["openInterest"].fillna(0).sum())
            atm_put_oi  += float(atm_puts["openInterest"].fillna(0).sum())

        if total_call_vol + total_put_vol < 10:
            return None   # insufficient liquidity

        pcr          = total_put_vol / max(total_call_vol, 1.0)
        atm_oi_ratio = atm_call_oi  / max(atm_put_oi,  1.0)
        atm_iv       = float(np.mean(atm_iv_samples)) if atm_iv_samples else 0.0

        # ── IV Rank: position of today's IV within 52-week HV range ──────────
        iv_rank = _compute_iv_rank(ticker, atm_iv)

        unusual_call_vol = (pcr < 0.5 and total_call_vol > 500)

        # ── Composite score (0–1) ─────────────────────────────────────────────
        score = 0.0
        reasons: list[str] = []

        if pcr < 0.7:
            score += 0.35
            reasons.append(f"PCR {pcr:.2f} (bullish call skew)")
        if atm_oi_ratio > 1.5:
            score += 0.35
            reasons.append(f"ATM OI ratio {atm_oi_ratio:.1f}x calls vs puts")
        if iv_rank < 30:
            score += 0.30
            reasons.append(f"IV Rank {iv_rank:.0f} (options cheap — breakout unpriced)")
        if unusual_call_vol:
            score = min(score + 0.10, 1.0)
            reasons.append(f"Unusual call vol ({total_call_vol:,.0f} vs {total_put_vol:,.0f} puts)")

        desc = (
            f"PCR={pcr:.2f} | OI-ratio={atm_oi_ratio:.1f}x | "
            f"IV-Rank={iv_rank:.0f} | ATM-IV={atm_iv:.1f}%"
            + (f" — {'; '.join(reasons)}" if reasons else " — no bullish signals")
        )

        return OptionsFlowResult(
            symbol=symbol,
            pcr=round(pcr, 3),
            atm_oi_ratio=round(atm_oi_ratio, 2),
            iv_rank=round(iv_rank, 1),
            atm_iv=round(atm_iv, 1),
            unusual_call_vol=unusual_call_vol,
            composite_score=round(score, 3),
            description=desc,
        )

    except Exception:
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_iv_rank(ticker: yf.Ticker, atm_iv: float) -> float:
    """
    IV Rank = (current IV − 52w low) / (52w high − 52w low) × 100.
    Uses 20-day historical volatility as IV proxy for the full year.
    Returns 50 (neutral) when insufficient history.
    """
    if atm_iv <= 0:
        return 50.0
    try:
        hist = ticker.history(period="1y", auto_adjust=True)
        if len(hist) < 30:
            return 50.0
        hv = hist["Close"].pct_change().dropna().rolling(20).std() * (252 ** 0.5) * 100
        hv = hv.dropna()
        if len(hv) < 5:
            return 50.0
        hv_min = float(hv.min())
        hv_max = float(hv.max())
        if hv_max <= hv_min:
            return 50.0
        rank = (atm_iv - hv_min) / (hv_max - hv_min) * 100
        return float(max(0.0, min(100.0, rank)))
    except Exception:
        return 50.0
