from abc import ABC, abstractmethod
import pandas as pd


class BaseStrategy(ABC):
    def __init__(self, symbol: str, params: dict = None):
        self.symbol = symbol
        self.params = params or {}

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> str:
        """Return 'buy', 'sell', or 'hold'."""
        ...

    def validate_data(self, df: pd.DataFrame, min_rows: int) -> bool:
        return df is not None and len(df) >= min_rows
