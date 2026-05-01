"""
Partial-exit order execution via Alpaca.

Entry + two exit legs submitted as separate orders:
  1. Market buy  — all shares
  2. Limit sell  — partial_shares at 2R target (GTC)
  3. Trailing stop sell — trail_shares with trail_price = 2×ATR (GTC)

Note: no hard stop-loss on the full position before the partial fills.
Monitor positions or add a manual stop order separately.
"""

from __future__ import annotations

from loguru import logger

from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import (
    MarketOrderRequest,
    LimitOrderRequest,
    TrailingStopOrderRequest,
)

from broker.alpaca_client import AlpacaClient
from risk.trade_setup import TradeSetup


class BracketOrderExecutor:
    def __init__(self, client: AlpacaClient) -> None:
        self._client = client

    def place(self, setup: TradeSetup) -> dict | None:
        """
        Submit entry + partial exit + trailing stop for `setup`.
        Returns a result dict with order details, or None on failure.
        Skips gracefully when the market is closed.
        """
        if not self._client.is_market_open():
            logger.warning(f"Market closed — orders skipped for {setup.symbol}")
            return None

        if setup.shares < 1:
            logger.warning(f"Invalid share count ({setup.shares}) for {setup.symbol}")
            return None

        try:
            # ── 1. Market buy — full position ──────────────────────────────
            buy_req = MarketOrderRequest(
                symbol=setup.symbol,
                qty=setup.shares,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
            )
            buy_order = self._client.trading_client.submit_order(buy_req)
            logger.info(
                f"Buy submitted — {setup.symbol} {setup.shares} sh @ ~${setup.entry_price:.2f} "
                f"| order_id={buy_order.id}"
            )

            # ── 2. Limit sell — partial_shares at 2R ──────────────────────
            partial_order_id = None
            if setup.partial_shares >= 1:
                limit_req = LimitOrderRequest(
                    symbol=setup.symbol,
                    qty=setup.partial_shares,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    limit_price=round(setup.target_price, 2),
                )
                partial_order = self._client.trading_client.submit_order(limit_req)
                partial_order_id = str(partial_order.id)
                logger.info(
                    f"Partial exit — {setup.symbol} {setup.partial_shares} sh "
                    f"limit ${setup.target_price:.2f} | order_id={partial_order_id}"
                )

            # ── 3. Trailing stop — trail_shares ───────────────────────────
            trail_order_id = None
            if setup.trail_shares >= 1:
                trail_req = TrailingStopOrderRequest(
                    symbol=setup.symbol,
                    qty=setup.trail_shares,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    trail_price=round(setup.trail_atr, 2),
                )
                trail_order = self._client.trading_client.submit_order(trail_req)
                trail_order_id = str(trail_order.id)
                logger.info(
                    f"Trailing stop — {setup.symbol} {setup.trail_shares} sh "
                    f"trail ${setup.trail_atr:.2f} | order_id={trail_order_id}"
                )

            return {
                "buy_order_id":     str(buy_order.id),
                "partial_order_id": partial_order_id,
                "trail_order_id":   trail_order_id,
                "symbol":           setup.symbol,
                "shares":           setup.shares,
                "partial_shares":   setup.partial_shares,
                "trail_shares":     setup.trail_shares,
                "entry":            setup.entry_price,
                "stop_loss":        setup.stop_loss,
                "partial_target":   setup.target_price,
                "trail_atr":        setup.trail_atr,
                "score":            setup.score,
            }

        except Exception as exc:
            logger.error(f"Order submission failed for {setup.symbol}: {exc}")
            return None
