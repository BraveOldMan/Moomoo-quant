# -*- coding: utf-8 -*-
"""漂移守卫：固定 active_weights() 的"加权因子全集"，并强制分类。

设计目的：回测 _score 是实盘 signals 的平行实现，二者可能悄悄漂移（如曾经的
capital 因子）。本测试把"加权因子全集"钉死，并要求每个因子被显式归类为：
  - BACKTESTABLE：回测 _score 能从历史复刻（与实盘同源，或文档化的历史代理）；
  - FORWARD_ONLY：实盘独有、无历史回放，只能 forward_ic_from_log 前向校准。

一旦有人给实盘 active_weights() 加入新加权因子而未在此登记，本测试即失败，
强制开发者明确归类并（若属 BACKTESTABLE）同步到 backtest._score。
"""

import dataclasses

from hk_strategy.config import StrategyConfig

# 回测 _score 能从历史复刻的因子。
BACKTESTABLE = {"turnover", "momentum", "rs", "capital", "short"}

# 实盘独有、无历史回放 → 须前向校准的因子（含港股特有的 hk_status 实时市场状态）。
FORWARD_ONLY = {
    "broker",
    "orb",
    "vwap",
    "order_flow",
    "dark_pool_proxy",
    "obi",
    "book_pressure",
    "book_spread",
    "book_slippage",
    "l2_imbalance",
    "hk_status",
    "intraday_flow",
    "option_iv",
}

ALL_FACTOR_FLAGS = dict(
    use_broker_signal=True,
    use_orb=True,
    use_rs=True,
    use_vwap_signal=True,
    use_order_flow=True,
    use_dark_pool_proxy=True,
    use_order_book_imbalance=True,
    use_order_book_pressure=True,
    use_order_book_metrics=True,
    use_l2_imbalance_tracker=True,
    use_hk_status_signal=True,
    use_intraday_flow=True,
    use_short_metrics=True,
    use_option_iv=True,
)


def _all_factors_weights() -> set[str]:
    cfg = dataclasses.replace(StrategyConfig(), **ALL_FACTOR_FLAGS)
    return set(cfg.active_weights().keys())


def test_backtestable_and_forward_only_are_disjoint() -> None:
    assert BACKTESTABLE.isdisjoint(FORWARD_ONLY)


def test_active_weights_universe_is_fully_classified() -> None:
    keys = _all_factors_weights()
    registry = BACKTESTABLE | FORWARD_ONLY
    missing = keys - registry  # 新增但未分类 → 必须显式归类并同步回测
    extra = registry - keys  # 登记了但 active_weights 已不产出 → 清理
    assert not missing, (
        "发现未分类的新加权因子，请归入 BACKTESTABLE（并同步 backtest._score）"
        f" 或 FORWARD_ONLY（前向校准）: {sorted(missing)}"
    )
    assert not extra, (
        f"登记表中已不存在于 active_weights 的因子，请清理: {sorted(extra)}"
    )
