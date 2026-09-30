import numpy as np
import pandas as pd
import pytest

from tradelab.data import align_market_data, generate_synthetic
from tradelab.strategies import STRATEGIES, make_overlay
from tradelab.strategies.base import rank_select, state_machine
from tradelab.strategies.calendar import turn_of_month_mask
from tradelab.strategies.pairs import adf_tstat
from tradelab.validation import lookahead_check

ALL = list(STRATEGIES.values()) + [make_overlay(STRATEGIES["trend_tsmom"], "regime"),
                                    make_overlay(STRATEGIES["xs_momentum"], "voltarget")]


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.name)
def test_weights_shape_and_limits(cls, synth):
    w = cls().generate_weights(synth)
    assert w.shape == synth.close.shape
    assert list(w.columns) == synth.assets
    assert np.isfinite(w.to_numpy()).all()
    gross = w.abs().sum(axis=1)
    max_gross = 1.5 if cls.name == "vol_managed" else 1.0
    assert gross.max() <= max_gross + 1e-9
    if cls.long_only:
        assert (w.to_numpy() >= -1e-12).all()
    assert gross.iloc[400:].gt(0).any(), "strategy never trades"


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.name)
def test_no_lookahead(cls, synth):
    assert lookahead_check(cls(), synth)["passed"]


def test_lookahead_check_catches_a_cheater(synth):
    from tradelab.strategies.base import Strategy

    class Cheat(Strategy):
        name = "cheat"
        default_params = {}

        def generate_weights(self, data):
            fwd = data.close.shift(-1) / data.close - 1  # tomorrow's return
            return (fwd > 0).astype(float) / len(data.assets)

    assert not lookahead_check(Cheat(), synth)["passed"]


def test_grid_respects_constraints():
    for params in STRATEGIES["donchian_breakout"].grid(100):
        assert params["exit"] < params["entry"]
    for params in STRATEGIES["xs_momentum"].grid(100):
        assert params["skip"] < params["lookback"]
    assert len(STRATEGIES["xs_momentum"].grid(5)) == 5


def test_applicability_single_asset(synth):
    one = synth.subset(["A"])
    applicable = {n for n, c in STRATEGIES.items() if c.applicable(one)[0]}
    assert applicable == {"trend_tsmom", "donchian_breakout", "rsi_pullback", "vol_managed", "turn_of_month"}


def test_state_machine():
    idx = pd.RangeIndex(6)
    e = pd.DataFrame({"x": [0, 1, 0, 0, 1, 0]}, index=idx).astype(bool)
    x = pd.DataFrame({"x": [0, 0, 0, 1, 0, 0]}, index=idx).astype(bool)
    assert state_machine(e, x)["x"].tolist() == [0, 1, 1, 0, 1, 1]


def test_rank_select_top_fraction():
    s = pd.DataFrame([[1, 2, 3, 4, np.nan]], columns=list("abcde"))
    assert rank_select(s, 0.5).iloc[0].tolist() == [False, False, True, True, False]
    assert rank_select(s, 0.25, top=False).iloc[0].tolist() == [True, False, False, False, False]


def test_turn_of_month_window():
    idx = pd.bdate_range("2024-01-24", "2024-02-08")
    m = pd.Series(turn_of_month_mask(idx, 2, 3), index=idx)
    # decision on Jan 26 (Fri) -> next bday Jan 29 is 3rd-to-last bday of Jan -> out
    assert not m["2024-01-26"]
    # decision on Jan 29 -> next bday Jan 30 is 2nd-to-last -> in
    assert m["2024-01-29"]
    # decision on Feb 5 -> next bday Feb 6 is 4th bday of Feb -> out
    assert m["2024-02-02"] and not m["2024-02-05"]


def test_adf_distinguishes_stationary_from_random_walk():
    rng = np.random.default_rng(0)
    noise = rng.normal(size=1000)
    walk = np.cumsum(noise)
    ou = np.zeros(1000)
    for i in range(1, 1000):
        ou[i] = 0.8 * ou[i - 1] + noise[i]
    assert adf_tstat(ou) < -5 < adf_tstat(walk)


def test_pairs_finds_planted_cointegration():
    tickers = ["P1", "P2", "Q1", "Q2", "BENCH"]
    raw = generate_synthetic(tickers, "2012-01-01", "2018-12-31", seed=3, benchmark="BENCH", cointegrated_pairs=2)
    d = align_market_data(raw, tickers[:-1], "BENCH", "2012-01-01", "2018-12-31")
    from tradelab.backtest import run_backtest
    from tradelab.costs import COST_PRESETS
    from tradelab.metrics import sharpe_ratio
    from tradelab.strategies.pairs import formation_schedule
    sched = formation_schedule(np.log(d.close), 252, 63, 2, -3.0)
    hits = [any({a, b} == {"P1", "P2"} for a, b, *_ in pairs) for _, pairs in sched]
    assert np.mean(hits) > 0.8, "planted pair P1/P2 not detected at most formation dates"
    w = STRATEGIES["pairs_stat_arb"]().generate_weights(d)
    assert (w["P1"] * w["P2"] < 0).any(), "planted pair never traded as a spread"
    r = run_backtest(w, d, COST_PRESETS["zero"]).returns.iloc[300:]
    assert sharpe_ratio(r) > 0.5
