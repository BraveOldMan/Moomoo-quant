# -*- coding: utf-8 -*-
"""analysis 纯统计函数单测。"""

import pytest

from hk_strategy.analysis import (
    information_coefficient,
    quantile_returns,
    summarize_ic,
)


def test_ic_perfect_negative():
    factor = [1, 2, 3, 4, 5]
    fwd = [5, 4, 3, 2, 1]
    ic = information_coefficient(factor, fwd, method="spearman")
    assert ic == pytest.approx(-1.0)


def test_ic_perfect_positive():
    factor = [1, 2, 3, 4, 5]
    fwd = [1, 2, 3, 4, 5]
    assert information_coefficient(factor, fwd, method="spearman") == pytest.approx(1.0)


def test_ic_handles_ties():
    factor = [1, 1, 2, 2]
    fwd = [1, 2, 3, 4]
    ic = information_coefficient(factor, fwd, method="spearman")
    assert -1.0 <= ic <= 1.0


def test_ic_too_few_points():
    assert information_coefficient([1], [1]) == 0.0


def test_summarize_ic_effective_negative():
    summ = summarize_ic(-0.08, n=100)
    assert "有效" in summ.interpretation


def test_summarize_ic_insufficient_sample():
    summ = summarize_ic(-0.5, n=5)
    assert "样本不足" in summ.interpretation


def test_quantile_returns_monotonic():
    # 因子越大未来收益越小 → 分位组收益应递减
    factor = list(range(10))
    fwd = [10 - x for x in range(10)]
    q = quantile_returns(factor, fwd, n_quantiles=5)
    means = [q[k] for k in sorted(q)]
    assert means == sorted(means, reverse=True)


def test_quantile_returns_insufficient():
    assert quantile_returns([1, 2], [1, 2], n_quantiles=5) == {}
