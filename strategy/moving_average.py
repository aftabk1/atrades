import pandas as pd
from .base import BaseStrategy


class MovingAverageCrossStrategy(BaseStrategy):
    """Golden/death cross strategy using fast and slow SMAs."""

    def __init__(self, symbol: str, fast: int = 10, slow: int = 30):
        super().__init__(symbol, {"fast": fast, "slow": slow})
        self.fast = fast
        self.slow = slow

    def generate_signal(self, df: pd.DataFrame) -> str:
        if not self.validate_data(df, self.slow + 1):
            return "hold"

        df = df.copy()
        df["sma_fast"] = df["close"].rolling(self.fast).mean()
        df["sma_slow"] = df["close"].rolling(self.slow).mean()

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        if prev["sma_fast"] <= prev["sma_slow"] and curr["sma_fast"] > curr["sma_slow"]:
            return "buy"
        if prev["sma_fast"] >= prev["sma_slow"] and curr["sma_fast"] < curr["sma_slow"]:
            return "sell"
        return "hold"
