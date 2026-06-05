# -*- coding: utf-8 -*-
"""实盘二次确认防护单测（ALLOW_REAL_TRADING 门槛，无需 OpenD）。"""

import pytest

from hk_strategy import main
from hk_strategy.config import StrategyConfig


def _clear_trade_env(monkeypatch):
    for name in ("TRADE_ENV", "ALLOW_REAL_TRADING", "TRADE_PASSWORD"):
        monkeypatch.delenv(name, raising=False)


def test_from_env_defaults_to_simulate_without_real_flag(monkeypatch):
    # Arrange：不设任何交易环境变量
    _clear_trade_env(monkeypatch)

    # Act
    cfg = StrategyConfig.from_env()

    # Assert：默认模拟，实盘开关关闭
    assert cfg.trd_env == "SIMULATE"
    assert cfg.allow_real_trading is False


@pytest.mark.parametrize(
    "value,expected",
    [
        ("yes", True),
        ("YES", True),
        ("true", True),
        ("1", True),
        ("no", False),
        ("", False),
        ("0", False),
    ],
)
def test_allow_real_trading_parsing(monkeypatch, value, expected):
    # Arrange
    _clear_trade_env(monkeypatch)
    monkeypatch.setenv("ALLOW_REAL_TRADING", value)

    # Act / Assert
    assert StrategyConfig.from_env().allow_real_trading is expected


def test_run_rejects_real_without_allow_flag(monkeypatch):
    # Arrange：请求实盘但未设二次确认开关
    _clear_trade_env(monkeypatch)
    monkeypatch.setenv("TRADE_ENV", "REAL")
    monkeypatch.setenv("TRADE_PASSWORD", "irrelevant")

    # Act / Assert：连接 OpenD 之前即拒绝启动
    with pytest.raises(RuntimeError, match="ALLOW_REAL_TRADING"):
        main.run()


def test_run_requires_password_when_real_allowed(monkeypatch):
    # Arrange：实盘开关已开，但缺少解锁密码
    _clear_trade_env(monkeypatch)
    monkeypatch.setenv("TRADE_ENV", "REAL")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "yes")

    # Act / Assert
    with pytest.raises(RuntimeError, match="TRADE_PASSWORD"):
        main.run()
