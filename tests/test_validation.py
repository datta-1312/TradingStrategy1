import pandas as pd

from tradelab.costs import COST_PRESETS
from tradelab.strategies.base import Strategy
from tradelab.validation import (BacktestSettings, market_regimes, robustness_score, run_grid, select_params,
                                 walk_forward, walk_forward_windows)


def test_walk_forward_windows_are_sequential_and_disjoint():
    idx = pd.bdate_range("2005-01-01", "2020-12-31")
    wins = walk_forward_windows(idx, idx[0], 5, 1)
    assert len(wins) == 11
    for a, b in zip(wins, wins[1:]):
        assert a["test_end"] < b["test_start"]
        assert a["train_end"] < a["test_start"]
    anchored = walk_forward_windows(idx, idx[0], 5, 1, anchored=True)
    assert all(w["train_start"] == idx[0] for w in anchored)


class Flip(Strategy):
    """Long in 2012-2015, wrong-way later: tests that selection only uses training data."""

    name = "flip"
    default_params = {"mode": 0}
    param_grid = {"mode": [0, 1]}

    def generate_weights(self, data):
        return pd.DataFrame(0.5 if self.params["mode"] == 0 else 0.0, index=data.index, columns=data.assets)


def test_selection_uses_only_training_window(synth):
    s = BacktestSettings(COST_PRESETS["zero"])
    runs = run_grid(Flip, synth, s, 10)
    # pick the window where the benchmark-ish assets fell: selection must favour cash (mode=1) there
    r = runs[0].result.returns
    worst_year = r.groupby(r.index.year).sum().idxmin()
    best_year = r.groupby(r.index.year).sum().idxmax()
    assert runs[select_params(runs, Flip.param_grid, f"{worst_year}-01-01", f"{worst_year}-12-31", 252, 0)].params["mode"] == 1
    assert runs[select_params(runs, Flip.param_grid, f"{best_year}-01-01", f"{best_year}-12-31", 252, 0)].params["mode"] == 0


def test_walk_forward_charges_switching_and_starts_at_first_test(synth):
    s = BacktestSettings(COST_PRESETS["us_etf"])
    runs = run_grid(Flip, synth, s, 10)
    wins = walk_forward_windows(synth.index, synth.index[250], 2, 1)
    wf = walk_forward(runs, Flip.param_grid, synth, s, wins, "flip")
    assert wf.returns.index[0] >= wins[0]["test_start"]
    assert len(wf.windows) == len(wins)
    assert wf.targets.loc[: wins[0]["test_start"] - pd.Timedelta(days=1)].abs().to_numpy().sum() == 0


def test_robustness_score_spike_vs_plateau():
    plateau = pd.DataFrame({"full_sharpe": [0.9, 1.0, 0.95, 0.85], "is_sharpe": [1, 1, 1, 1],
                            "oos_sharpe": [0.8, 0.9, 0.7, 0.6]})
    spike = pd.DataFrame({"full_sharpe": [-0.2, 1.5, 0.0, -0.1], "is_sharpe": [0, 2, 0, 0],
                          "oos_sharpe": [0, 0.1, 0, 0]})
    assert robustness_score(plateau)["median_to_best"] > 0.8
    assert robustness_score(spike)["median_to_best"] == 0.0


def test_market_regimes_labels(synth):
    reg = market_regimes(synth.benchmark)
    assert set(reg["trend"].unique()) <= {"bull", "bear", "sideways", "n/a"}
    assert (reg["trend"].iloc[:149] == "n/a").all()
