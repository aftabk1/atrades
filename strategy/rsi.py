import pandas as pd
import ta
from .base import BaseStrategy


class RSIStrategy(BaseStrategy):
    """Mean-reversion strategy using RSI overbought/oversold levels."""

    def __init__(self, symbol: str, period: int = 14, oversold: float = 30, overbought: float = 70):
        super().__init__(symbol, {"period": period, "oversold": oversold, "overbought": overbought})
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def generate_signal(self, df: pd.DataFrame) -> str:
        if not self.validate_data(df, self.period + 1):
            return "hold"

        rsi = ta.momentum.RSIIndicator(close=df["close"], window=self.period).rsi()
        current_rsi = rsi.iloc[-1]

        if current_rsi < self.oversold:
            return "buy"
        if current_rsi > self.overbought:
            return "sell"
        return "hold"
