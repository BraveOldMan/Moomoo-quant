# -*- coding: utf-8 -*-
"""港股增强因子信号层单测，不连接 OpenD。"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import moomoo as ft

from hk_strategy.config import StrategyConfig
from hk_strategy.signals import SignalCalculator


class _Data:
    def __init__(self) -> None:
        self._book_calls = 0

    def get_market_snapshot(self, _code: str):
        return ft.RET_OK, pd.DataFrame(
            [{"last_price": 10.0, "turnover": 8_000_000.0, "turnover_rate": 1.0}]
        )

    def get_capital_distribution(self, _code: str):
        return ft.RET_ERROR, pd.DataFrame()

    def request_history_kline(self, code: str, **kwargs):
        ktype = kwargs.get("ktype")
        if code == "HK.TEST" and ktype == ft.KLType.K_DAY:
            return (
                ft.RET_OK,
                pd.DataFrame(
                    {
                        "close": [10.0, 10.1, 10.2, 10.3, 10.4, 10.5],
                        "high": [10.1, 10.2, 10.3, 10.4, 10.5, 10.6],
                        "low": [9.9, 10.0, 10.1, 10.2, 10.3, 10.4],
                        "volume": [1000, 1000, 1000, 1000, 1000, 1000],
                    }
                ),
                None,
            )
        if code == "HK.TEST" and ktype == ft.KLType.K_1M:
            return (
                ft.RET_OK,
                pd.DataFrame(
                    {
                        "time_key": [
                            "2026-06-05 11:45:00",
                            "2026-06-05 11:59:00",
                            "2026-06-05 13:00:00",
                            "2026-06-05 13:15:00",
                        ],
                        "close": [10.0, 10.1, 10.1, 10.2],
                        "high": [10.0, 10.1, 10.1, 10.2],
                        "low": [10.0, 10.1, 10.1, 10.2],
                        "volume": [100, 100, 100, 100],
                    }
                ),
                None,
            )
        return ft.RET_ERROR, pd.DataFrame(), None

    def get_order_book(self, _code: str, num: int):
        self._book_calls += 1
        bid_size = 500 if self._book_calls == 1 else 900
        ask_size = 500 if self._book_calls == 1 else 300
        bid = [(10.0 - i * 0.01, bid_size, 1, {}) for i in range(num)]
        ask = [(10.0 + i * 0.01, ask_size, 1, {}) for i in range(num)]
        return ft.RET_OK, {"Bid": bid, "Ask": ask}

    def get_broker_queue(self, _code: str):
        bid = pd.DataFrame([{"broker": "B1"}])
        ask = pd.DataFrame([{"broker": "A1"}, {"broker": "A2"}, {"broker": "A3"}])
        return ft.RET_OK, bid, ask

    def get_rt_ticker(self, _code: str, _num: int):
        today = datetime.now(ZoneInfo("Asia/Hong_Kong")).date().isoformat()
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "code": "HK.TEST",
                    "time": f"{today} 10:00:00",
                    "price": 400.0,
                    "volume": 3000,
                    "turnover": 1_200_000.0,
                    "ticker_direction": "BUY",
                    "sequence": 1,
                }
            ]
        )


def test_hk_signals_record_lunch_broker_pressure_and_futures_block() -> None:
    cfg = StrategyConfig(
        use_broker_signal=True,
        use_broker_gate=True,
        use_order_book_pressure=True,
        use_order_book_metrics=True,
        use_l2_imbalance_tracker=True,
        use_dark_pool_proxy=True,
        use_lunch_continuation=True,
        use_hk_futures_filter=True,
    )
    data = _Data()
    calc = SignalCalculator(data, cfg)

    first = calc.calculate("HK.TEST", last_price=10.0)
    second = calc.calculate("HK.TEST", last_price=10.0)

    assert first is not None
    assert second is not None
    assert first.scores["broker"] == 75.0
    assert "book_pressure" not in first.scores
    assert second.scores["book_pressure"] < 50.0
    assert {"book_spread", "book_slippage"}.issubset(second.scores)
    assert "l2_imbalance" in second.scores
    assert second.scores["dark_pool_proxy"] == 0.0
    assert cfg.active_weights()["book_spread"] == 0.0
    assert cfg.active_weights()["book_slippage"] == 0.0
    assert cfg.active_weights()["l2_imbalance"] == 0.0
    assert cfg.active_weights()["dark_pool_proxy"] == 0.0
    assert second.scores["lunch_continuation"] < 50.0
    assert "恒指/国指期货过滤数据缺失" in second.buy_block_reasons
