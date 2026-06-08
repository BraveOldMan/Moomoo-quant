# -*- coding: utf-8 -*-
"""观察列表加载单测（WATCHLIST 环境变量优先 + watchlist.txt 回退，无需 OpenD）。"""

from datetime import date

import pandas as pd

import moomoo as ft

from us_strategy.config import StrategyConfig, _load_watchlist
from us_strategy.main import (
    _FailureAlertGate,
    _account_snapshot_text,
    _buy_failure_once_key,
    _buy_alert_message,
    _sync_broker_positions,
    _snapshot_display_name,
    _should_suppress_buy_failure_alert,
    _should_ignore_unheld_sell,
)
from us_strategy.persistence import PositionRecord


def test_env_takes_precedence_over_file(tmp_path, monkeypatch):
    # Arrange：文件与环境变量同时存在
    wl = tmp_path / "watchlist.txt"
    wl.write_text("US.FILE1\nUS.FILE2\n", encoding="utf-8")
    monkeypatch.setenv("WATCHLIST_FILE", str(wl))
    monkeypatch.setenv("WATCHLIST", "US.ENV1, US.ENV2")

    # Act / Assert：环境变量优先，文件被忽略
    assert _load_watchlist() == ("US.ENV1", "US.ENV2")


def test_file_fallback_parses_comments_blanks_and_dedups(tmp_path, monkeypatch):
    # Arrange：含注释、空行、行内注释、逗号分隔、重复项
    wl = tmp_path / "watchlist.txt"
    wl.write_text(
        "# 头部注释\n\nUS.MRVL    # 行内注释\nUS.AMD, US.NVDA\nUS.MRVL\n",  # 重复应去除
        encoding="utf-8",
    )
    monkeypatch.delenv("WATCHLIST", raising=False)
    monkeypatch.setenv("WATCHLIST_FILE", str(wl))

    # Act
    result = _load_watchlist()

    # Assert：去重保序，注释/空行被剔除
    assert result == ("US.MRVL", "US.AMD", "US.NVDA")


def test_returns_empty_when_no_env_and_no_file(tmp_path, monkeypatch):
    # Arrange：环境变量为空且文件不存在
    monkeypatch.delenv("WATCHLIST", raising=False)
    monkeypatch.setenv("WATCHLIST_FILE", str(tmp_path / "missing.txt"))

    # Act / Assert：回退到仅 IPO 扫描
    assert _load_watchlist() == ()


def test_blank_env_falls_back_to_file(tmp_path, monkeypatch):
    # Arrange：WATCHLIST 为空白字符串应视为未设置
    wl = tmp_path / "watchlist.txt"
    wl.write_text("US.TSLA\n", encoding="utf-8")
    monkeypatch.setenv("WATCHLIST", "   ")
    monkeypatch.setenv("WATCHLIST_FILE", str(wl))

    # Act / Assert
    assert _load_watchlist() == ("US.TSLA",)


def test_failure_alert_gate_throttles_by_event_and_code() -> None:
    gate = _FailureAlertGate(cooldown_s=300.0)

    assert gate.should_send("买入失败", "US.AAPL", now=1000.0) is True
    assert gate.should_send("买入失败", "US.AAPL", now=1200.0) is False
    assert gate.should_send("卖出失败", "US.AAPL", now=1200.0) is True
    assert gate.should_send("买入失败", "US.MSFT", now=1200.0) is True
    assert gate.should_send("买入失败", "US.AAPL", now=1300.0) is True


def test_zero_cooldown_failure_alert_gate_never_throttles() -> None:
    gate = _FailureAlertGate(cooldown_s=0.0)

    assert gate.should_send("买入失败", "US.AAPL", now=1000.0) is True
    assert gate.should_send("买入失败", "US.AAPL", now=1001.0) is True


def test_failure_alert_gate_sends_once_for_deterministic_reason() -> None:
    gate = _FailureAlertGate(cooldown_s=0.0)

    once_key = _buy_failure_once_key("已达最大持仓数 8")
    assert once_key == "已达最大持仓数"
    assert gate.should_send("买入未执行", "US.AAPL", once_key=once_key) is True
    assert gate.should_send("买入未执行", "US.AAPL", once_key=once_key) is False
    assert gate.should_send("买入未执行", "US.MSFT", once_key=once_key) is True
    assert gate.should_send("卖出失败", "US.AAPL", once_key=once_key) is True


def test_buy_failure_once_key_only_matches_max_position_block() -> None:
    assert _buy_failure_once_key("已达最大持仓数 8") == "已达最大持仓数"
    assert _buy_failure_once_key("订单未成交或超时") is None


def test_ignore_unheld_sell_only_when_strategy_and_broker_are_flat() -> None:
    assert _should_ignore_unheld_sell(False, 0) is True
    assert _should_ignore_unheld_sell(False, 100) is False
    assert _should_ignore_unheld_sell(True, 0) is False


def test_snapshot_display_name_combines_name_and_code() -> None:
    assert _snapshot_display_name({"name": "苹果"}, "US.AAPL") == "苹果（US.AAPL）"


def test_snapshot_display_name_falls_back_to_code() -> None:
    assert _snapshot_display_name({"name": "--"}, "US.AAPL") == "US.AAPL"


def test_buy_alert_message_is_explicit() -> None:
    message = _buy_alert_message(
        display="苹果（US.AAPL）",
        signal_price=200.0,
        lot_size=1,
        cfg=StrategyConfig(order_lots_per_trade=1),
        decision_reason="综合风险低，价格动量向上",
        result="已成交",
        detail="成交数量：qty=1",
    )

    assert "标的：苹果（US.AAPL）" in message
    assert "信号：BUY" in message
    assert "计划下单：1手，qty=1，lot_size=1" in message
    assert "执行结果：已成交" in message
    assert "成交数量：qty=1" in message


def test_buy_alert_message_appends_account_snapshot() -> None:
    message = _buy_alert_message(
        display="苹果（US.AAPL）",
        signal_price=200.0,
        lot_size=1,
        cfg=StrategyConfig(order_lots_per_trade=1),
        decision_reason="综合风险低，价格动量向上",
        result="已成交",
        detail="成交数量：qty=1",
        account_snapshot="账户快照：资产净值 1,020,434.23 美元",
    )

    assert message.endswith("账户快照：资产净值 1,020,434.23 美元")


def test_account_snapshot_text_formats_key_fields() -> None:
    snapshot = _account_snapshot_text(_DummyAccountData())

    assert "\n" not in snapshot
    assert snapshot.startswith("账户快照：资产净值 1,020,434.23 美元")
    assert "今日盈亏 +3.68" in snapshot
    assert "今日盈亏比例 +0.00%" in snapshot
    assert "持仓盈亏 +3.75" in snapshot
    assert "持仓市值 8,947.12" in snapshot
    assert "最大购买力 2,031,607.13" in snapshot
    assert "维持保证金 3,578.85" in snapshot
    assert "现金 1,011,173.89" in snapshot
    assert "剩余流动性 1,016,855.38" in snapshot


def test_cash_shortfall_buy_failure_alert_is_suppressed() -> None:
    assert _should_suppress_buy_failure_alert("固定1手下单资金不足") is True
    assert _should_suppress_buy_failure_alert("单批预算不足") is True
    assert _should_suppress_buy_failure_alert("订单未成交或超时") is False
    assert _should_suppress_buy_failure_alert("下单接口失败") is False


def test_sync_broker_positions_restores_missing_local_position() -> None:
    strategy = _DummyStrategy()
    store = _DummyStore()
    cfg = StrategyConfig(order_lots_per_trade=1, entry_tranches=2)

    _sync_broker_positions(
        data=_DummyBrokerPositions(),
        strategy=strategy,
        store=store,
        cfg=cfg,
        saved={},
    )

    assert store.saved["US.AAPL"].cost_price == 123.4
    assert store.saved["US.AAPL"].qty == 1.0
    assert store.saved["US.AAPL"].tranches_bought == 1
    assert store.saved["US.AAPL"].peak_price == 125.0
    assert isinstance(store.saved["US.AAPL"].buy_date, date)
    assert strategy.restored["US.AAPL"]["qty"] == 1.0


def test_sync_broker_positions_raises_tranches_from_broker_qty() -> None:
    strategy = _DummyStrategy()
    store = _DummyStore()
    cfg = StrategyConfig(order_lots_per_trade=1, entry_tranches=2)
    saved = {
        "US.AAPL": PositionRecord(
            code="US.AAPL",
            cost_price=120.0,
            buy_date=date(2026, 6, 8),
            tranches_bought=1,
            peak_price=130.0,
            qty=1.0,
        )
    }

    _sync_broker_positions(
        data=_DummyBrokerPositions(qty=2.0),
        strategy=strategy,
        store=store,
        cfg=cfg,
        saved=saved,
    )

    assert store.saved["US.AAPL"].qty == 2.0
    assert store.saved["US.AAPL"].tranches_bought == 2
    assert store.saved["US.AAPL"].buy_date == date(2026, 6, 8)


class _DummyBrokerPositions:
    def __init__(self, qty: float = 1.0) -> None:
        self._qty = qty

    def position_list_query(self):
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "code": "US.AAPL",
                    "qty": self._qty,
                    "cost_price": 123.4,
                    "price": 125.0,
                }
            ]
        )


class _DummyAccountData:
    def accinfo_query(self):
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "net_assets": 1_020_434.23,
                    "market_val": 8_947.12,
                    "power": 2_031_607.13,
                    "maintenance_margin": 3_578.85,
                    "cash": 1_011_173.89,
                }
            ]
        )

    def position_list_query(self):
        return ft.RET_OK, pd.DataFrame(
            [
                {"today_pl_val": 1.68, "pl_val": 2.0},
                {"today_pl_val": 2.0, "pl_val": 1.75},
            ]
        )


class _DummyStrategy:
    def __init__(self) -> None:
        self.restored = {}

    def restore_position(self, **kwargs) -> None:
        self.restored[kwargs["code"]] = kwargs


class _DummyStore:
    def __init__(self) -> None:
        self.saved = {}

    def save(self, record) -> None:
        self.saved[record.code] = record
