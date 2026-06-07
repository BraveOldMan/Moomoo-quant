"""Market-specific module loading for symmetric US/HK research."""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Any


@dataclass(frozen=True)
class MarketBundle:
    """Strategy modules and defaults for one market."""

    market: str
    config: Any
    features: Any
    backtest: Any
    default_benchmark: str


def load_market(market: str) -> MarketBundle:
    """Load US or HK strategy modules without changing strategy defaults."""

    normalized = market.lower()
    if normalized not in {"us", "hk"}:
        raise ValueError("market must be 'us' or 'hk'")
    package = f"{normalized}_strategy"
    config_mod = import_module(f"{package}.config")
    features_mod = import_module(f"{package}.features")
    backtest_mod = import_module(f"{package}.backtest")
    cfg = config_mod.StrategyConfig.from_env()
    return MarketBundle(
        market=normalized,
        config=cfg,
        features=features_mod,
        backtest=backtest_mod,
        default_benchmark=cfg.backtest_benchmark,
    )

