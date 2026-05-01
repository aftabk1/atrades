class PositionSizer:
    def __init__(self, max_position_fraction: float = 0.10):
        self.max_position_fraction = max_position_fraction

    def shares_for_budget(self, budget: float, price: float) -> float:
        """Return whole share count fitting within budget."""
        if price <= 0:
            return 0.0
        return max(0.0, budget // price)

    def budget(self, portfolio_value: float) -> float:
        return portfolio_value * self.max_position_fraction
