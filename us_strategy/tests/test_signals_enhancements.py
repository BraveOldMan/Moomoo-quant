# -*- coding: utf-8 -*-
"""增强因子信号层单测，不连接 OpenD。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import moomoo as ft

from us_strategy.config import StrategyConfig
from us_strategy.signals import SignalCalculator


class _Data:
    def get_market_snapshot(self, code: str):
        if code.startswith("US.C") or code.startswith("US.P"):
            return ft.RET_OK, pd.DataFrame(
                [
                    {
                        "option_implied_volatility": 80.0
                        if code.startswith("US.P")
                        else 50.0,
                        "option_open_interest": 2000
                        if code.startswith("US.P")
                        else 1000,
                    }
                ]
            )
        return ft.RET_OK, pd.DataFrame(
            [{"last_price": 10.0, "turnover": 2_000_000.0, "turnover_rate": 1.0}]
        )

    def get_capital_distribution(self, _code: str):
        return ft.RET_ERROR, pd.DataFrame()

    def request_history_kline(self, code: str, **_kwargs):
        if code == "US.TEST":
            return (
                ft.RET_OK,
                pd.DataFrame(
                    {
                        "close": [10.0, 10.2, 10.4, 10.6, 10.8, 11.0],
                        "high": [10.1, 10.3, 10.5, 10.7, 10.9, 11.1],
                        "low": [9.9, 10.1, 10.3, 10.5, 10.7, 10.9],
                        "volume": [1000, 1000, 1000, 1000, 1000, 1000],
                    }
                ),
                None,
            )
        return ft.RET_ERROR, pd.DataFrame(), None

    def get_order_book(self, _code: str, num: int):
        bid = [(10.0 - i * 0.01, 1000 - i * 10, 1, {}) for i in range(num)]
        ask = [(10.0 + i * 0.01, 500 + i * 10, 1, {}) for i in range(num)]
        return ft.RET_OK, {"Bid": bid, "Ask": ask}

    def get_rt_ticker(self, _code: str, _num: int):
        today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "code": "US.TEST",
                    "time": f"{today} 10:00:00",
                    "price": 10.0,
                    "volume": 20_000,
                    "turnover": 200_000.0,
                    "ticker_direction": "SELL",
                    "sequence": 1,
                }
            ]
        )

    def get_option_expiration_date(self, _code: str):
        return ft.RET_OK, pd.DataFrame(
            [{"strike_time": "2026-06-19", "option_expiry_date_distance": 10}]
        )

    def get_option_chain(self, _code: str, _start: str, _end: str):
        return ft.RET_OK, pd.DataFrame(
            [
                {"code": "US.CALL", "strike_price": 10.0, "option_type": "CALL"},
                {"code": "US.PUT", "strike_price": 10.0, "option_type": "PUT"},
            ]
        )


def test_us_signals_log_multi_obi_and_option_warning_without_weight() -> None:
    cfg = StrategyConfig(
        use_order_book_imbalance=True,
        use_order_book_metrics=True,
        use_l2_imbalance_tracker=True,
        use_dark_pool_proxy=True,
        use_option_iv=True,
        use_macro_filter=True,
    )
    result = SignalCalculator(_Data(), cfg).calculate("US.TEST", last_price=10.0)

    assert result is not None
    assert {"obi_l1", "obi_l3", "obi_l5", "obi_l10", "obi"}.issubset(result.scores)
    assert {"book_spread", "book_slippage"}.issubset(result.scores)
    assert "l2_imbalance" in result.scores
    assert result.scores["dark_pool_proxy"] == 100.0
    assert cfg.active_weights()["book_spread"] == 0.0
    assert cfg.active_weights()["book_slippage"] == 0.0
    assert cfg.active_weights()["l2_imbalance"] == 0.0
    assert cfg.active_weights()["dark_pool_proxy"] == 0.0
    assert result.scores["option_iv"] >= cfg.option_warning_score
    assert result.risk_warnings
    # option_iv 与其它因子一致：use 标志开启即以默认 0 权重进入 active_weights，
    # 校准后赋予 w_option_iv 即可参与综合评分（修复前被静默丢弃）。
    assert cfg.active_weights()["option_iv"] == 0.0
    assert result.buy_block_reasons == ["纳指/VIX宏观过滤数据缺失"]
