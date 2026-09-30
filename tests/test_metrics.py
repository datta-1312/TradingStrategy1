import math

import numpy as np
import pandas as pd
import pytest

from tradelab import metrics


def _series(values):
    return pd.Series(values, index=pd.bdate_range("2020-01-01", periods=len(values)))


def test_max_drawdown_known_path():
    r = _series([0.1, -0.5, 0.2, 1.0])
    assert metrics.max_drawdown(r)["max_drawdown"] == pytest.approx(0.5)


def test_sharpe_and_cagr():
    rng = np.random.default_rng(0)
    r = _series(rng.normal(0.0004, 0.01, 2520))
    sr = metrics.sharpe_ratio(r)
    assert sr == pytest.approx(r.mean() / r.std() * math.sqrt(252))
    assert metrics.cagr(_series([0.0] * 252)) == pytest.approx(0.0)
    assert metrics.cagr(_series([0.01] + [0.0] * 251)) == pytest.approx(0.01)


def test_psr_and_dsr_ordering():
    rng = np.random.default_rng(1)
    good = _series(rng.normal(0.0008, 0.01, 2520))
    noise = _series(rng.normal(0.0, 0.01, 2520))
    assert metrics.probabilistic_sharpe(good) > 0.95
    assert metrics.probabilistic_sharpe(good) > metrics.probabilistic_sharpe(noise)
    # more trials -> bigger haircut
    d10 = metrics.deflated_sharpe(good, 10, 0.02)
    d1000 = metrics.deflated_sharpe(good, 1000, 0.02)
    assert d1000 < d10 <= metrics.probabilistic_sharpe(good)


def test_expected_max_sharpe_grows_with_trials():
    assert metrics.expected_max_sharpe(1, 0.02) == 0.0
    assert metrics.expected_max_sharpe(100, 0.02) > metrics.expected_max_sharpe(10, 0.02) > 0


def test_bootstrap_ci_contains_point_estimate():
    rng = np.random.default_rng(2)
    r = _series(rng.normal(0.0005, 0.01, 1500))
    lo, hi = metrics.bootstrap_sharpe_ci(r, n_boot=300)
    assert lo < metrics.sharpe_ratio(r) < hi


def test_summary_with_benchmark():
    rng = np.random.default_rng(3)
    b = _series(rng.normal(0.0003, 0.01, 1000))
    r = 0.5 * b + _series(rng.normal(0.0001, 0.002, 1000))
    m = metrics.performance_summary(r, b)
    assert m["beta"] == pytest.approx(0.5, abs=0.05)
    assert 0 < m["correlation"] <= 1
