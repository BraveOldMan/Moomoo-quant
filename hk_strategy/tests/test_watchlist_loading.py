# -*- coding: utf-8 -*-
"""观察列表加载单测（WATCHLIST 环境变量优先 + watchlist.txt 回退，无需 OpenD）。"""

import pandas as pd

import moomoo as ft

from hk_strategy.config import StrategyConfig, _load_watchlist
from hk_strategy.main import (
    _FailureAlertGate,
    _OnceEventGate,
    _account_snapshot_text,
    _buy_failure_once_key,
    _buy_alert_message,
    _snapshot_display_name,
    _should_suppress_buy_failure_alert,
    _should_ignore_unheld_sell,
    _tradable_watchlist,
)


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


def test_tradable_watchlist_excludes_benchmark_symbol() -> None:
    cfg = StrategyConfig(
        watchlist=("HK.00700", "HK.800000", "HK.02800"),
        trade_excluded_symbols=("HK.800000",),
    )

    assert _tradable_watchlist(cfg) == ("HK.00700", "HK.02800")


def test_failure_alert_gate_throttles_by_event_and_code() -> None:
    gate = _FailureAlertGate(cooldown_s=300.0)

    assert gate.should_send("买入失败", "HK.00700", now=1000.0) is True
    assert gate.should_send("买入失败", "HK.00700", now=1200.0) is False
    assert gate.should_send("卖出失败", "HK.00700", now=1200.0) is True
    assert gate.should_send("买入失败", "HK.09988", now=1200.0) is True
    assert gate.should_send("买入失败", "HK.00700", now=1300.0) is True


def test_zero_cooldown_failure_alert_gate_never_throttles() -> None:
    gate = _FailureAlertGate(cooldown_s=0.0)

    assert gate.should_send("买入失败", "HK.00700", now=1000.0) is True
    assert gate.should_send("买入失败", "HK.00700", now=1001.0) is True


def test_failure_alert_gate_sends_once_for_deterministic_reason() -> None:
    gate = _FailureAlertGate(cooldown_s=0.0)

    once_key = _buy_failure_once_key("已达最大持仓数 13")
    assert once_key == "已达最大持仓数"
    assert gate.should_send("买入未执行", "HK.00700", once_key=once_key) is True
    assert gate.should_send("买入未执行", "HK.00700", once_key=once_key) is False
    assert gate.should_send("买入未执行", "HK.09988", once_key=once_key) is True
    assert gate.should_send("卖出失败", "HK.00700", once_key=once_key) is True


def test_once_event_gate_dedupes_ipo_key_events() -> None:
    gate = _OnceEventGate()

    assert gate.should_send("发现今日IPO", "HK.06680") is True
    assert gate.should_send("发现今日IPO", "HK.06680") is False
    assert gate.should_send("IPO首次分析", "HK.06680") is True
    assert gate.should_send("发现今日IPO", "HK.09999") is True


def test_buy_failure_once_key_only_matches_max_position_block() -> None:
    assert _buy_failure_once_key("已达最大持仓数 13") == "已达最大持仓数"
    assert _buy_failure_once_key("订单未成交或超时") is None


def test_ignore_unheld_sell_only_when_strategy_and_broker_are_flat() -> None:
    assert _should_ignore_unheld_sell(False, 0) is True
    assert _should_ignore_unheld_sell(False, 100) is False
    assert _should_ignore_unheld_sell(True, 0) is False


def test_snapshot_display_name_combines_name_and_code() -> None:
    assert _snapshot_display_name({"name": "腾讯控股"}, "HK.00700") == "腾讯控股（HK.00700）"


def test_snapshot_display_name_falls_back_to_code() -> None:
    assert _snapshot_display_name({"name": "N/A"}, "HK.00700") == "HK.00700"


def test_buy_alert_message_is_explicit() -> None:
    message = _buy_alert_message(
        display="宁德时代（HK.03750）",
        signal_price=688.5,
        lot_size=100,
        cfg=StrategyConfig(order_lots_per_trade=1),
        decision_reason="综合风险低，价格动量向上",
        result="未执行",
        detail="原因：固定1手下单资金不足",
    )

    assert "标的：宁德时代（HK.03750）" in message
    assert "信号：BUY" in message
    assert "计划下单：1手，qty=100，lot_size=100" in message
    assert "执行结果：未执行" in message
    assert "原因：固定1手下单资金不足" in message


def test_buy_alert_message_appends_account_snapshot() -> None:
    message = _buy_alert_message(
        display="腾讯控股（HK.00700）",
        signal_price=448.8,
        lot_size=100,
        cfg=StrategyConfig(order_lots_per_trade=1),
        decision_reason="综合风险低，价格动量向上",
        result="已成交",
        detail="成交数量：qty=100",
        account_snapshot="账户快照：资产净值 1,020,434.23 港元",
    )

    assert message.endswith("账户快照：资产净值 1,020,434.23 港元")


def test_account_snapshot_text_formats_key_fields() -> None:
    snapshot = _account_snapshot_text(_DummyAccountData())

    assert "\n" not in snapshot
    assert snapshot.startswith("账户快照：资产净值 1,020,434.23 港元")
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
