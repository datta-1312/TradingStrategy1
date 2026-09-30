import numpy as np
import pandas as pd
import pytest

from tradelab.backtest import extract_trades, run_backtest
from tradelab.costs import COST_PRESETS, CostModel

from .conftest import make_data

ZERO = COST_PRESETS["zero"]


def test_buy_and_hold_matches_asset_return_same_close():
    d = make_data({"X": [100, 101, 99, 105, 110]})
    tgt = pd.DataFrame(1.0, index=d.index, columns=["X"])
    res = run_backtest(tgt, d, ZERO, execution="same_close")
    assert res.equity.iloc[-1] == pytest.approx(110 / 100)


def test_signal_never_earns_its_own_bar():
    # Target set at the close of day 2 must not earn day 2's +50% jump in any execution mode.
    d = make_data({"X": [100, 100, 150, 150, 150]})
    tgt = pd.DataFrame(0.0, index=d.index, columns=["X"])
    tgt.iloc[2:] = 1.0
    for mode in ("same_close", "next_close", "next_open"):
        res = run_backtest(tgt, d, ZERO, execution=mode)
        assert res.equity.iloc[-1] == pytest.approx(1.0), mode


def test_next_open_earns_intraday_but_not_overnight():
    d = make_data({"X": [100, 100, 120, 120]}, opens={"X": [100, 100, 110, 120]})
    tgt = pd.DataFrame(0.0, index=d.index, columns=["X"])
    tgt.iloc[1:] = 1.0  # decided at close of bar 1, filled at open of bar 2 (110)
    res = run_backtest(tgt, d, ZERO, execution="next_open")
    assert res.equity.iloc[-1] == pytest.approx(120 / 110)


def test_linear_costs_charged_on_turnover():
    d = make_data({"X": [100.0] * 6}, volume=0)
    tgt = pd.DataFrame(0.0, index=d.index, columns=["X"])
    tgt.iloc[1:3] = 1.0  # buy then sell
    costs = CostModel("t", commission_bps=5, slippage_bps=5, impact_coef=0, borrow_bps=0, financing_spread_bps=0)
    res = run_backtest(tgt, d, costs, execution="same_close")
    assert res.turnover.sum() == pytest.approx(2.0)
    assert res.equity.iloc[-1] == pytest.approx((1 - 0.001) ** 2)


def test_no_phantom_turnover_when_target_unchanged():
    d = make_data({"X": [100, 110, 90, 120, 100], "Y": [100, 95, 105, 100, 102]})
    tgt = pd.DataFrame(0.5, index=d.index, columns=["X", "Y"])
    res = run_backtest(tgt, d, COST_PRESETS["us_etf"], execution="same_close")
    assert res.turnover.iloc[1:].sum() == 0.0
    # drifted weights: X outperforms on day 1
    assert res.weights.iloc[2]["X"] > 0.5


def test_short_pays_borrow_and_gains_when_price_falls():
    d = make_data({"X": [100, 90, 90]}, volume=0)
    tgt = pd.DataFrame(-1.0, index=d.index, columns=["X"])
    borrow = CostModel("b", 0, 0, 0, 0, 0, borrow_bps=252 * 10, financing_spread_bps=0)
    res = run_backtest(tgt, d, borrow, execution="same_close")
    # day 1: +10% short gain, then weights drift; borrow 10 bps/day on short notional
    assert res.returns.iloc[1] > 0.09
    assert res.returns.iloc[2] < 0


def test_cash_earns_risk_free():
    d = make_data({"X": [100.0] * 253})
    tgt = pd.DataFrame(0.0, index=d.index, columns=["X"])
    res = run_backtest(tgt, d, ZERO, risk_free_rate=0.0252, periods_per_year=252)
    assert res.returns.iloc[1] == pytest.approx(0.0001)


def test_impact_increases_with_size():
    d = make_data({"X": list(np.linspace(100, 110, 60))}, volume=1_000)
    tgt = pd.DataFrame(0.0, index=d.index, columns=["X"])
    tgt.iloc[30:] = 1.0
    small = run_backtest(tgt, d, COST_PRESETS["us_etf"], capital=1e3)
    big = run_backtest(tgt, d, COST_PRESETS["us_etf"], capital=1e7)
    assert big.cost.sum() > small.cost.sum()
    assert big.participation.max() > 1  # 10M into a 100k/day market is flagged


def test_extract_trades_spells_and_flip():
    idx = pd.bdate_range("2020-01-01", periods=8)
    w = pd.DataFrame({"X": [0, 1, 1, 0, -1, -1, 1, 0]}, index=idx, dtype=float)
    c = pd.DataFrame({"X": [0, 0.01, 0.02, 0, 0.01, -0.03, 0.01, 0]}, index=idx)
    t = extract_trades(w, c)
    assert list(t["direction"]) == ["long", "short", "long"]
    assert list(t["bars"]) == [2, 2, 1]
    assert t["pnl"].iloc[0] == pytest.approx(0.03)
    assert t["pnl"].iloc[1] == pytest.approx(-0.02)
