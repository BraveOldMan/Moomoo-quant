# -*- coding: utf-8 -*-
from .config import Signal, StrategyConfig
from .data_access import DataAccess
from .strategy import Decision, IPOStrategy
from .signals import SignalCalculator, SignalResult
from .trader import Trader
from .monitor import RealtimeMonitor
from .backtest import BacktestEngine, BacktestResult
from .analysis import FactorAnalyzer, forward_ic_from_log
from .persistence import PositionRecord, PositionStore, SignalLogRecord, SignalLogStore

__version__ = "1.9.0"

__all__ = [
    "__version__",
    "Signal",
    "StrategyConfig",
    "DataAccess",
    "Decision",
    "IPOStrategy",
    "SignalCalculator",
    "SignalResult",
    "Trader",
    "RealtimeMonitor",
    "BacktestEngine",
    "BacktestResult",
    "FactorAnalyzer",
    "forward_ic_from_log",
    "PositionRecord",
    "PositionStore",
    "SignalLogRecord",
    "SignalLogStore",
]
