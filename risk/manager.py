from loguru import logger
from broker.alpaca_client import AlpacaClient
from .position_sizer import PositionSizer
import config


class RiskManager:
    def __init__(self, client: AlpacaClient):
        self._client = client
        self._sizer = PositionSizer(config.MAX_POSITION_SIZE)
        self._max_risk = config.MAX_PORTFOLIO_RISK

    def approve_buy(self, symbol: str, price: float) -> tuple[bool, float]:
        """Return (approved, qty). qty=0 means rejected."""
        portfolio_value = self._client.get_portfolio_value()
        cash = self._client.get_cash()
        budget = min(self._sizer.budget(portfolio_value), cash)

        if budget <= 0:
            logger.warning(f"Buy rejected for {symbol}: insufficient cash")
            return False, 0.0

        existing = self._client.get_position(symbol)
        if existing:
            logger.info(f"Already holding {symbol}, skipping duplicate buy")
            return False, 0.0

        qty = self._sizer.shares_for_budget(budget, price)
        if qty < 1:
            logger.warning(f"Buy rejected for {symbol}: budget too small for 1 share at {price}")
            return False, 0.0

        max_loss = portfolio_value * self._max_risk
        potential_loss = qty * price  # simplified: full position as worst case
        if potential_loss > max_loss * 10:
            logger.warning(f"Buy rejected for {symbol}: position size exceeds risk tolerance")
            return False, 0.0

        return True, qty

    def approve_sell(self, symbol: str) -> tuple[bool, float]:
        """Return (approved, qty) based on current held position."""
        position = self._client.get_position(symbol)
        if not position:
            logger.info(f"No position in {symbol} to sell")
            return False, 0.0
        qty = float(position.qty)
        return True, qty

    def check_drawdown(self, stop_loss_pct: float = 0.05) -> bool:
        """Return True if portfolio is within acceptable drawdown."""
        account = self._client.get_account()
        equity = float(account.equity)
        last_equity = float(account.last_equity)
        if last_equity <= 0:
            return True
        drawdown = (last_equity - equity) / last_equity
        if drawdown > stop_loss_pct:
            logger.warning(f"Drawdown {drawdown:.2%} exceeds limit {stop_loss_pct:.2%} — halting trades")
            return False
        return True
