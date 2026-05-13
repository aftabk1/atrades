"""
Live position monitor for A1TRADES.

Three responsibilities called from runner.py:

1. sync_open_trades()
   - For each 'open' trade: check if partial limit filled
     → cancel hard stop → place trailing stop → set status='partial_exit'
   - For each trade: check if stop/trail/limit fully exited
     → close trade in DB

2. ratchet_trailing_stops()
   - For each 'partial_exit' trade with a trailing stop order:
     fetch latest close + ATR; compute new trail level;
     if improved, cancel old order and place new trailing stop at tighter distance.

3. check_circuit_breaker()
   - Compare current portfolio value to stored start-of-day value.
   - Return True (halt) if daily loss exceeds MAX_DAILY_LOSS_PCT.
"""

from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pandas as pd
import yfinance as yf
from loguru import logger

from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    QueryOrderStatus,
    TrailingStopOrderRequest,
)

import config
from broker.alpaca_client import AlpacaClient
from notifications.whatsapp import notify
from data.store import (
    close_trade,
    get_open_trades,
    update_trade_fill,
    upgrade_to_trailing,
)


# ── sync_open_trades ──────────────────────────────────────────────────────────

def sync_open_trades(client: AlpacaClient) -> None:
    """
    Reconcile DB open trades against live Alpaca order/position state.
    Upgrades stop → trailing stop when partial limit fills.
    Closes trades when stop or trailing stop fires.
    """
    trades = get_open_trades()
    if not trades:
        return

    tc = client.trading_client
    for trade in trades:
        symbol       = trade["symbol"]
        buy_id       = trade["buy_order_id"]
        stop_id      = trade["stop_order_id"]
        partial_id   = trade["partial_order_id"]
        trail_id     = trade["trail_order_id"]
        trail_shares = trade["trail_shares"] or 0
        trail_atr    = trade["trail_atr"]   or 0
        status       = trade["status"]

        # ── Check if position still exists at all ─────────────────────────
        position = client.get_position(symbol)

        # ── Fill price not yet recorded ───────────────────────────────────
        if trade["fill_price"] is None and buy_id:
            try:
                order = tc.get_order_by_id(buy_id)
                if order.status == OrderStatus.FILLED and order.filled_avg_price:
                    fill_ts = order.filled_at.isoformat() if order.filled_at else None
                    update_trade_fill(buy_id, float(order.filled_avg_price),
                                      fill_ts, stop_id)
                    logger.info(f"{symbol}: recorded fill @ ${order.filled_avg_price}")
            except Exception as exc:
                logger.debug(f"{symbol}: fill check error — {exc}")

        # ── Position closed (stop or trail fired) ─────────────────────────
        if position is None:
            _handle_closed_position(tc, trade)
            continue

        # ── status='open': check if partial limit has filled ──────────────
        if status == "open" and partial_id and stop_id:
            try:
                partial_order = tc.get_order_by_id(partial_id)
                if partial_order.status == OrderStatus.FILLED:
                    logger.info(f"{symbol}: partial limit filled — upgrading to trailing stop")
                    _upgrade_stop_to_trail(tc, trade)
            except Exception as exc:
                logger.debug(f"{symbol}: partial order check error — {exc}")

        # ── status='partial_exit': check if trailing stop has fired ───────
        elif status == "partial_exit" and trail_id:
            try:
                trail_order = tc.get_order_by_id(trail_id)
                if trail_order.status == OrderStatus.FILLED:
                    exit_price = float(trail_order.filled_avg_price or 0)
                    close_trade(buy_id, exit_price, "trailing_stop")
                    logger.info(f"{symbol}: trailing stop filled @ ${exit_price:.2f} — trade closed")
                    notify(f"TRADE CLOSED: {symbol} via Trailing Stop @ ${exit_price:.2f}")
            except Exception as exc:
                logger.debug(f"{symbol}: trail order check error — {exc}")


def _handle_closed_position(tc, trade: dict) -> None:
    """Position is gone — figure out which order triggered it and close the DB record."""
    symbol = trade["symbol"]
    buy_id = trade["buy_order_id"]

    for order_id, reason in [
        (trade["stop_order_id"],    "stop_loss"),
        (trade["trail_order_id"],   "trailing_stop"),
    ]:
        if not order_id:
            continue
        try:
            order = tc.get_order_by_id(order_id)
            if order.status == OrderStatus.FILLED:
                exit_price = float(order.filled_avg_price or 0)
                close_trade(buy_id, exit_price, reason)
                logger.info(f"{symbol}: closed via {reason} @ ${exit_price:.2f}")
                notify(f"TRADE CLOSED: {symbol} via {reason.replace('_', ' ').title()} "
                       f"@ ${exit_price:.2f}")
                return
        except Exception:
            pass

    # Partial limit filled but trail_shares remain — Alpaca position briefly
    # shows zero during settlement. Treat as partial exit, not a full close.
    partial_id = trade["partial_order_id"]
    if partial_id and (trade["trail_shares"] or 0) > 0:
        try:
            order = tc.get_order_by_id(partial_id)
            if order.status == OrderStatus.FILLED:
                logger.info(f"{symbol}: partial fill detected with position gone (settlement lag) "
                            f"— upgrading to trailing stop")
                _upgrade_stop_to_trail(tc, trade)
                return
        except Exception:
            pass

    # Full close at partial target (trail_shares == 0): treat as target_hit
    if partial_id:
        try:
            order = tc.get_order_by_id(partial_id)
            if order.status == OrderStatus.FILLED:
                exit_price = float(order.filled_avg_price or 0)
                close_trade(buy_id, exit_price, "target_hit")
                logger.info(f"{symbol}: closed via target_hit @ ${exit_price:.2f}")
                notify(f"TRADE CLOSED: {symbol} via Target Hit @ ${exit_price:.2f}")
                return
        except Exception:
            pass
    # Position gone and no known order matched — try to recover exit price
    # from the most recent filled sell order for this symbol before giving up.
    exit_price = 0.0
    exit_reason = "unknown"
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus as _QOS
        recent = tc.get_orders(filter=GetOrdersRequest(
            symbol=symbol, status=_QOS.ALL, limit=10
        ))
        for o in recent:
            if (o.side == OrderSide.SELL
                    and o.status == OrderStatus.FILLED
                    and o.filled_avg_price):
                exit_price  = float(o.filled_avg_price)
                exit_reason = "unknown_recovered"
                logger.info(
                    f"{symbol}: recovered exit price ${exit_price:.2f} "
                    f"from order {str(o.id)[:16]}"
                )
                break
    except Exception as exc:
        logger.debug(f"{symbol}: exit price recovery failed: {exc}")

    close_trade(buy_id, exit_price, exit_reason)
    logger.warning(
        f"{symbol}: position closed, exit order not identified"
        + (f" — recovered price ${exit_price:.2f}" if exit_price else " — no price recovered, exit=0")
    )
    notify(
        f"TRADE CLOSED: {symbol} — exit order unidentified"
        + (f", recovered price ${exit_price:.2f}" if exit_price else ", exit price unknown")
    )


def _upgrade_stop_to_trail(tc, trade: dict) -> None:
    """Cancel hard stop, place trailing stop for trail_shares."""
    symbol       = trade["symbol"]
    stop_id      = trade["stop_order_id"]
    buy_id       = trade["buy_order_id"]
    trail_shares = trade["trail_shares"] or 0
    trail_atr    = trade["trail_atr"]   or 0

    if trail_shares < 1 or trail_atr <= 0:
        return

    try:
        tc.cancel_order_by_id(stop_id)
        logger.info(f"{symbol}: hard stop {stop_id} cancelled")
    except Exception as exc:
        logger.warning(f"{symbol}: could not cancel stop {stop_id}: {exc}")

    try:
        trail_req = TrailingStopOrderRequest(
            symbol=symbol,
            qty=trail_shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            trail_price=round(trail_atr, 2),
        )
        trail_order = tc.submit_order(trail_req)
        upgrade_to_trailing(buy_id, str(trail_order.id), stop_id)
        logger.info(
            f"{symbol}: trailing stop placed — {trail_shares} sh "
            f"trail ${trail_atr:.2f} | id={trail_order.id}"
        )
    except Exception as exc:
        logger.error(f"{symbol}: trailing stop placement failed: {exc}")


# ── ratchet_trailing_stops ────────────────────────────────────────────────────

def ratchet_trailing_stops(client: AlpacaClient) -> None:
    """
    For each partial_exit trade, tighten the trailing stop if ATR-based
    calculation gives a better (higher) stop than the current trail distance.
    """
    trades = [t for t in get_open_trades() if t["status"] == "partial_exit"]
    if not trades:
        return

    logger.info(f"Ratchet check: {len(trades)} position(s) in trailing phase")
    tc = client.trading_client

    for trade in trades:
        symbol    = trade["symbol"]
        trail_id  = trade["trail_order_id"]
        fill_px   = trade["fill_price"] or trade["entry"]
        trail_atr = trade["trail_atr"] or 0

        if not trail_id or trail_atr <= 0:
            continue

        try:
            trail_order = tc.get_order_by_id(trail_id)
            if trail_order.status != OrderStatus.ACCEPTED:
                continue

            # Fetch latest daily bar
            df = _fetch_daily(symbol, days=20)
            if df is None or len(df) < 15:
                continue

            close     = float(df["close"].iloc[-1])
            prev_low  = float(df["low"].iloc[-2])
            atr14     = _atr14(df)

            # New trail level: how far below current close should the stop be?
            new_trail_dist = round(2 * atr14, 2)
            current_trail  = float(trail_order.trail_price or trail_atr)

            # Ratchet: only tighten (reduce trail distance) — locks in gains
            if new_trail_dist < current_trail:
                tc.cancel_order_by_id(trail_id)
                trail_req = TrailingStopOrderRequest(
                    symbol=symbol,
                    qty=trade["trail_shares"],
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    trail_price=new_trail_dist,
                )
                new_order = tc.submit_order(trail_req)
                from data.store import upgrade_to_trailing
                upgrade_to_trailing(trade["buy_order_id"], str(new_order.id), trail_id)
                logger.info(
                    f"{symbol}: trail ratcheted ${current_trail:.2f} → ${new_trail_dist:.2f} "
                    f"(ATR14={atr14:.2f})"
                )
        except Exception as exc:
            logger.debug(f"{symbol}: ratchet error — {exc}")


# ── circuit_breaker ───────────────────────────────────────────────────────────

_start_of_day_value: float | None = None
_start_of_day_date:  str          = ""


def check_circuit_breaker(client: AlpacaClient) -> bool:
    """
    Returns True if trading should be halted for the day.
    Halts when portfolio is down more than MAX_DAILY_LOSS_PCT from open.
    Records start-of-day value once per calendar day.
    """
    global _start_of_day_value, _start_of_day_date

    max_loss = getattr(config, "MAX_DAILY_LOSS_PCT", 0.04)
    today    = date.today().isoformat()

    try:
        current = client.get_portfolio_value()
    except Exception as exc:
        logger.warning(f"Circuit breaker: could not fetch portfolio value — {exc}")
        return False

    if _start_of_day_date != today:
        _start_of_day_value = current
        _start_of_day_date  = today
        logger.info(f"Circuit breaker: start-of-day portfolio = ${current:,.2f}")
        return False

    if _start_of_day_value and _start_of_day_value > 0:
        daily_loss_pct = (_start_of_day_value - current) / _start_of_day_value
        if daily_loss_pct >= max_loss:
            logger.warning(
                f"CIRCUIT BREAKER TRIGGERED — portfolio down "
                f"{daily_loss_pct:.1%} today "
                f"(${_start_of_day_value:,.0f} → ${current:,.0f}). "
                f"No new trades for the rest of the day."
            )
            return True

    return False


# ── reconcile_orphans ─────────────────────────────────────────────────────────

def reconcile_orphans(client: AlpacaClient) -> None:
    """
    For every open Alpaca position, verify that sell-side orders cover the
    full qty. If a position has no open sell orders at all, place a market
    stop at the DB stop_loss price (or a 5% floor) so it is never naked.
    """
    try:
        tc        = client.trading_client
        positions = {p.symbol: p for p in tc.get_all_positions()}
    except Exception as exc:
        logger.warning(f"reconcile_orphans: could not fetch positions — {exc}")
        return

    if not positions:
        return

    # Fetch ALL sell orders and filter to non-terminal statuses ourselves.
    # IMPORTANT: Alpaca puts stop/trailing-stop orders into 'held' status (not
    # 'open'), so QueryOrderStatus.OPEN misses them entirely — causing
    # reconcile_orphans to see 0 covered and place a new emergency stop every
    # scan cycle.  Using QueryOrderStatus.ALL and discarding terminal statuses
    # is the correct approach.
    _TERMINAL = {"filled", "canceled", "expired", "replaced", "done_for_day"}
    try:
        all_sell_orders = tc.get_orders(filter=GetOrdersRequest(
            status=QueryOrderStatus.ALL,
            side=OrderSide.SELL,
            limit=500,
        ))
    except Exception as exc:
        logger.warning(f"reconcile_orphans: could not fetch orders — {exc}")
        return

    sell_qty: dict[str, float] = {}
    for o in all_sell_orders:
        if str(o.status.value if hasattr(o.status, "value") else o.status) not in _TERMINAL:
            sell_qty[o.symbol] = sell_qty.get(o.symbol, 0) + float(o.qty or 0)

    db_trades = {t["symbol"]: t for t in get_open_trades()}

    for symbol, pos in positions.items():
        pos_qty = float(pos.qty)
        covered = sell_qty.get(symbol, 0)

        if covered >= pos_qty:
            continue  # fully covered

        uncovered = pos_qty - covered
        logger.warning(
            f"reconcile_orphans: {symbol} has {pos_qty} shares but only "
            f"{covered} covered by sell orders — placing emergency stop for {uncovered}"
        )

        # Determine stop price: use DB stop_loss if available, else 5% below current
        db = db_trades.get(symbol, {})
        stop_price = db.get("stop_loss") or 0
        current    = float(pos.current_price or pos.avg_entry_price)
        if not stop_price or stop_price <= 0:
            stop_price = round(current * 0.95, 2)

        try:
            from alpaca.trading.requests import StopOrderRequest
            stop_req = StopOrderRequest(
                symbol=symbol,
                qty=uncovered,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                stop_price=round(stop_price, 2),
            )
            order = tc.submit_order(stop_req)
            logger.info(
                f"reconcile_orphans: emergency stop placed for {symbol} "
                f"— {uncovered} sh @ stop ${stop_price:.2f} | id={order.id}"
            )
        except Exception as exc:
            logger.error(f"reconcile_orphans: failed to place stop for {symbol}: {exc}")


# ── helpers ───────────────────────────────────────────────────────────────────

def _fetch_daily(symbol: str, days: int = 30) -> pd.DataFrame | None:
    try:
        df = yf.download(symbol, period=f"{days}d", interval="1d",
                         progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0].lower() for c in df.columns]
        else:
            df.columns = [c.lower() for c in df.columns]
        return df.dropna() if len(df) >= 10 else None
    except Exception:
        return None


def _atr14(df: pd.DataFrame) -> float:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low  - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return float(tr.rolling(14).mean().iloc[-1])
