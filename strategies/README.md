# Strategies Folder

## Purpose

This folder contains **Lumibot trading strategy classes** for the Lawvisory automated portfolio management system. Each strategy implements a distinct risk profile and trading logic optimized for different investment objectives.

## Overview

Strategies in this folder are built using the **Lumibot framework** and are designed to:
- Execute automated trading on Interactive Brokers
- Implement distinct risk profiles (Conservative, Moderate, Balanced, Aggressive, Maximum Return)
- Select and trade stocks from the S&P 500 universe
- Use daily timeframe data for long-term holding and trading decisions
- Self-adapt and optimize portfolio composition over time

## Strategy Structure

Each strategy class should:
1. Inherit from Lumibot's `Strategy` base class
2. Implement stock selection logic based on the assigned risk profile
3. Define entry and exit conditions for trades
4. Include position sizing and risk management rules
5. Implement portfolio rebalancing logic
6. Adapt to changing market conditions

## Risk Profiles

### Conservative Strategy
- **Focus**: Capital preservation, low volatility
- **Stock Selection**: Blue-chip stocks, dividend-paying companies, defensive sectors
- **Risk Management**: Tight stop-losses, conservative position sizing

### Moderate Strategy
- **Focus**: Balanced growth with controlled risk
- **Stock Selection**: Established companies with consistent earnings
- **Risk Management**: Moderate position sizing, diversified sectors

### Balanced Strategy
- **Focus**: Equal balance between growth and stability
- **Stock Selection**: Mix of growth and value stocks
- **Risk Management**: Balanced position sizing across sectors

### Aggressive Strategy
- **Focus**: Significant growth potential
- **Stock Selection**: Growth stocks, emerging sectors, high momentum
- **Risk Management**: Higher position sizing, wider stop-losses

### Maximum Return Strategy
- **Focus**: Maximum capital appreciation
- **Stock Selection**: High-growth stocks, technology sector, emerging companies
- **Risk Management**: Maximum leverage, aggressive position sizing

## Data Source

All strategies use stock data from the `data/STOCKS/` directory, which contains CSV files with daily OHLCV data for S&P 500 stocks. The data is updated regularly using the `update_stocks.py` script.

## Usage

Strategies in this folder are designed to be:
- **Backtested** using Lumibot's backtesting engine
- **Deployed** to Interactive Brokers for live trading
- **Monitored** for performance and adaptation

## Example Structure

A typical strategy file should follow this structure:

```python
from lumibot.strategies import Strategy

class ConservativeStrategy(Strategy):
    def initialize(self):
        # Initialize parameters, risk profile settings
        pass
    
    def on_trading_iteration(self):
        # Main trading logic
        pass
    
    def select_stocks(self):
        # Stock selection based on risk profile
        pass
    
    def calculate_position_size(self, stock):
        # Position sizing logic
        pass
```

## Notes

- All strategies use **daily timeframe data** exclusively
- Strategies are designed to be **self-adapting** and improve over time
- Trading is **fully automated** with no manual intervention required
- Results from backtesting are stored in the `backtest_results/` folder

