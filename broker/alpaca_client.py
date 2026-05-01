from alpaca.trading.client import TradingClient
from alpaca.trading.models import Position, TradeAccount
from loguru import logger
import config


class AlpacaClient:
    def __init__(self):
        self._client = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=config.IS_PAPER,
        )

    def get_account(self) -> TradeAccount:
        return self._client.get_account()

    def get_portfolio_value(self) -> float:
        account = self.get_account()
        return float(account.portfolio_value)

    def get_cash(self) -> float:
        return float(self.get_account().cash)

    def get_position(self, symbol: str) -> Position | None:
        try:
            return self._client.get_open_position(symbol)
        except Exception:
            return None

    def get_all_positions(self) -> list[Position]:
        try:
            return self._client.get_all_positions()
        except Exception as exc:
            logger.error(f"Failed to fetch positions: {exc}")
            return []

    def is_market_open(self) -> bool:
        clock = self._client.get_clock()
        return clock.is_open

    @property
    def trading_client(self) -> TradingClient:
        return self._client
