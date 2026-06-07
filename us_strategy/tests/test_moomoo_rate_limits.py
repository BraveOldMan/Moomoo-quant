from __future__ import annotations

from hk_strategy.config import StrategyConfig as HKStrategyConfig
from moomoo_rate_limits import (
    DEFAULT_DATA_ACCESS_RATE_LIMIT,
    DEFAULT_OPTION_CHAIN_SLEEP_SECONDS,
    MOOMOO_RATE_LIMITS,
    rate_limit_for,
)
from tools import run_us_stock_daily_report
from us_strategy.config import StrategyConfig as USStrategyConfig


def test_snapshot_rate_limit_is_official() -> None:
    """Snapshot calls must keep the official 60 requests per 30 seconds rule."""

    rule = rate_limit_for("get_market_snapshot")

    assert rule.official is True
    assert rule.limit == 60
    assert rule.window_s == 30.0
    assert rule.min_interval_s == 0.5
    assert rule.request_size_limit == 400


def test_option_chain_rate_limit_is_stricter_than_snapshot() -> None:
    """Option chain calls are the bottleneck for the daily option report."""

    rule = rate_limit_for("get_option_chain")

    assert rule.official is True
    assert rule.limit == 10
    assert rule.window_s == 30.0
    assert rule.min_interval_s == DEFAULT_OPTION_CHAIN_SLEEP_SECONDS
    assert run_us_stock_daily_report.OPTION_API_SUCCESS_PAUSE_SECONDS == 3.0


def test_realtime_cache_getters_are_not_server_request_limited() -> None:
    """Subscribed real-time getters read OpenD cache instead of server pulls."""

    for interface in (
        "get_stock_quote",
        "get_order_book",
        "get_rt_ticker",
        "get_broker_queue",
    ):
        rule = rate_limit_for(interface)
        assert rule.limit is None
        assert rule.requires_subscription is True


def test_strategy_configs_use_conservative_bucket(monkeypatch) -> None:
    """US and HK configs should share the same conservative DataAccess bucket."""

    monkeypatch.delenv("API_RATE_LIMIT", raising=False)
    monkeypatch.delenv("API_RATE_WINDOW_S", raising=False)

    assert USStrategyConfig().api_rate_limit == DEFAULT_DATA_ACCESS_RATE_LIMIT
    assert HKStrategyConfig().api_rate_limit == DEFAULT_DATA_ACCESS_RATE_LIMIT
    assert USStrategyConfig.from_env().api_rate_window_s == 30.0
    assert HKStrategyConfig.from_env().api_rate_window_s == 30.0


def test_strategy_configs_allow_rate_env_override(monkeypatch) -> None:
    """Operators can lower the bucket without touching strategy code."""

    monkeypatch.setenv("API_RATE_LIMIT", "20")
    monkeypatch.setenv("API_RATE_WINDOW_S", "40")

    assert USStrategyConfig.from_env().api_rate_limit == 20
    assert USStrategyConfig.from_env().api_rate_window_s == 40.0
    assert HKStrategyConfig.from_env().api_rate_limit == 20
    assert HKStrategyConfig.from_env().api_rate_window_s == 40.0


def test_unpublished_repo_interfaces_are_marked_non_official() -> None:
    """Fallback entries must not pretend to be official moomoo page limits."""

    rule = MOOMOO_RATE_LIMITS["get_daily_short_volume"]

    assert rule.official is False
    assert rule.limit == 30
    assert rule.window_s == 30.0
