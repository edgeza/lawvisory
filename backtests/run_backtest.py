from pathlib import Path
import sys
from datetime import datetime

# --- MAKE PROJECT ROOT IMPORTABLE ---
# This adds the parent folder (lawvisory) to Python's import path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))
# ------------------------------------

from lumibot.backtesting import YahooDataBacktesting
from strategies.conservative import ConservativeStrategy


if __name__ == "__main__":
    # Choose your date range and starting cash
    backtesting_start = datetime(2020, 2, 2)
    backtesting_end = datetime(2024, 12, 31)
    budget = 100_000  # starting cash

    # Run the backtest using keyword arguments
    result = ConservativeStrategy.backtest(
        datasource_class=YahooDataBacktesting,
        backtesting_start=backtesting_start,
        backtesting_end=backtesting_end,
        budget=budget,
        benchmark_asset="SPY",
        name="conservative_strategy",
    )

    print(result)