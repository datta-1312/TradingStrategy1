"""Validation toolkit: look-ahead checks, parameter selection, IS/OOS and walk-forward
testing, sensitivity, cost/execution stress, regime and concentration analysis.

Design choice: each parameter set is simulated once over the full history (signals
are causal, which ``lookahead_check`` proves), and every window-specific statistic
is computed by slicing. Walk-forward results are then re-simulated from stitched
target weights so that switching costs between parameter sets are charged.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .backtest import BacktestResult, run_backtest
from .costs import CostModel
from .data import MarketData
from .metrics import max_drawdown, sharpe_ratio
from .strategies.base import Strategy


@dataclass
class BacktestSettings:
    costs: CostModel
    execution: str = "next_open"
    capital: float = 1_000_000.0
    risk_free_rate: float = 0.0
    periods_per_year: int = 252

    def run(self, targets: pd.DataFrame, data: MarketData, label: str = "", **overrides) -> BacktestResult:
        kw = dict(costs=self.costs, execution=self.execution, capital=self.capital,
                  risk_free_rate=self.risk_free_rate, periods_per_year=self.periods_per_year)
        kw.update(overrides)
        return run_backtest(targets, data, label=label, **kw)


@dataclass
class GridRun:
    params: dict
    label: str
    targets: pd.DataFrame
    result: BacktestResult


@dataclass
class WalkForwardResult:
    windows: list[dict]
    result: BacktestResult
    returns: pd.Series
    targets: pd.DataFrame
    start: pd.Timestamp


# ----------------------------------------------------------------- look-ahead


def lookahead_check(strategy: Strategy, data: MarketData, cuts=(0.45, 0.7, 0.9), tol: float = 1e-9) -> dict:
    """Recompute weights on truncated histories; any difference means the strategy peeks ahead."""
    full = strategy.generate_weights(data).fillna(0.0)
    worst = 0.0
    for frac in cuts:
        cut = data.index[int(len(data.index) * frac)]
        part = strategy.generate_weights(data.truncate(cut)).fillna(0.0)
        diff = (full.loc[:cut] - part.reindex(full.loc[:cut].index).fillna(0.0)).abs().to_numpy().max()
        worst = max(worst, float(diff))
    return {"passed": worst <= tol, "max_abs_diff": worst}


# ----------------------------------------------------------------- grid & selection


def run_grid(
    strategy_cls: type[Strategy], data: MarketData, settings: BacktestSettings, max_grid: int, seed: int = 0
) -> list[GridRun]:
    runs = []
    for params in strategy_cls.grid(max_grid, seed):
        strat = strategy_cls(**params)
        targets = strat.generate_weights(data)
        runs.append(GridRun(params, strat.label, targets, settings.run(targets, data, strat.label)))
    return runs


def _neighbours(runs: list[GridRun], grid: dict[str, list]) -> list[list[int]]:
    """Indices of runs that differ from each run by one step in exactly one grid dimension."""
    pos = []
    for r in runs:
        pos.append({k: grid[k].index(r.params[k]) if r.params[k] in grid[k] else -99 for k in grid})
    out = []
    for i, pi in enumerate(pos):
        nb = []
        for j, pj in enumerate(pos):
            if i == j:
                continue
            diffs = [abs(pi[k] - pj[k]) for k in grid]
            if sorted(diffs)[-1] == 1 and sum(d > 0 for d in diffs) == 1:
                nb.append(j)
        out.append(nb)
    return out


def window_sharpes(runs: list[GridRun], start, end, periods: int, rf: float) -> np.ndarray:
    return np.array([sharpe_ratio(r.result.returns.loc[start:end], periods, rf) for r in runs])


def select_params(
    runs: list[GridRun], grid: dict[str, list], start, end, periods: int, rf: float,
    neighbours: Optional[list[list[int]]] = None,
) -> int:
    """Pick the parameter set with the best *plateau* score on [start, end].

    Plateau score = min(own Sharpe, 0.5 * own + 0.5 * mean Sharpe of grid neighbours):
    an isolated peak (likely overfit) is pulled down towards its neighbours, while a
    weak setting is never pulled up by strong neighbours.
    """
    sh = window_sharpes(runs, start, end, periods, rf)
    neighbours = neighbours if neighbours is not None else _neighbours(runs, grid)
    score = np.array([
        min(sh[i], 0.5 * sh[i] + 0.5 * sh[nb].mean()) if nb else sh[i] for i, nb in enumerate(neighbours)
    ])
    return int(np.argmax(score))


def walk_forward_windows(
    index: pd.DatetimeIndex, eval_start: pd.Timestamp, train_years: float, test_years: float,
    anchored: bool = False,
) -> list[dict]:
    windows = []
    train_len = pd.DateOffset(days=int(round(train_years * 365.25)))
    test_len = pd.DateOffset(days=int(round(test_years * 365.25)))
    test_start = eval_start + train_len
    last = index[-1]
    while test_start < last:
        test_end = min(test_start + test_len, last + pd.Timedelta(days=1))
        train_start = eval_start if anchored else test_start - train_len
        windows.append({
            "train_start": train_start, "train_end": test_start - pd.Timedelta(days=1),
            "test_start": test_start, "test_end": test_end - pd.Timedelta(days=1),
        })
        test_start = test_end
    # Drop a final stub shorter than a quarter of a test window.
    if len(windows) > 1 and (windows[-1]["test_end"] - windows[-1]["test_start"]).days < 90 * test_years:
        windows[-2]["test_end"] = windows[-1]["test_end"]
        windows.pop()
    return windows


def walk_forward(
    runs: list[GridRun], grid: dict[str, list], data: MarketData, settings: BacktestSettings,
    windows: list[dict], label: str,
) -> WalkForwardResult:
    """Choose parameters on each training window, trade them on the following test window."""
    periods, rf = settings.periods_per_year, settings.risk_free_rate
    nb = _neighbours(runs, grid)
    stitched = pd.DataFrame(0.0, index=data.index, columns=data.assets)
    chosen_windows = []
    for w in windows:
        i = select_params(runs, grid, w["train_start"], w["train_end"], periods, rf, nb)
        seg = runs[i].targets.loc[w["test_start"]: w["test_end"]]
        stitched.loc[seg.index] = seg.reindex(columns=data.assets).fillna(0.0).to_numpy()
        train_sh = sharpe_ratio(runs[i].result.returns.loc[w["train_start"]: w["train_end"]], periods, rf)
        test_sh = sharpe_ratio(runs[i].result.returns.loc[w["test_start"]: w["test_end"]], periods, rf)
        chosen_windows.append({**w, "params": runs[i].params, "label": runs[i].label,
                               "train_sharpe": train_sh, "test_sharpe": test_sh})
    start = windows[0]["test_start"]
    res = settings.run(stitched, data, f"{label} [walk-forward]")
    return WalkForwardResult(chosen_windows, res, res.returns.loc[start:], stitched, start)


# ----------------------------------------------------------------- robustness


def parameter_sensitivity(runs: list[GridRun], eval_start, split, periods: int, rf: float) -> pd.DataFrame:
    rows = []
    for r in runs:
        full = r.result.returns.loc[eval_start:]
        rows.append({
            **{k: v for k, v in r.params.items()},
            "label": r.label,
            "is_sharpe": sharpe_ratio(r.result.returns.loc[eval_start:split], periods, rf),
            "oos_sharpe": sharpe_ratio(r.result.returns.loc[split:], periods, rf),
            "full_sharpe": sharpe_ratio(full, periods, rf),
            "full_max_dd": max_drawdown(full)["max_drawdown"],
        })
    return pd.DataFrame(rows)


def robustness_score(sensitivity: pd.DataFrame) -> dict:
    sh = sensitivity["full_sharpe"].to_numpy()
    best = float(sh.max()) if len(sh) else 0.0
    med = float(np.median(sh)) if len(sh) else 0.0
    ratio = max(0.0, med / best) if best > 0 else 0.0
    varied = len(sh) > 2 and sensitivity["is_sharpe"].std() > 0 and sensitivity["oos_sharpe"].std() > 0
    return {
        "grid_size": int(len(sh)),
        "best_sharpe": best,
        "median_sharpe": med,
        "median_to_best": ratio,
        "frac_positive": float((sh > 0).mean()) if len(sh) else 0.0,
        # Does in-sample ranking of parameter sets predict out-of-sample ranking?
        "is_oos_rank_corr": float(sensitivity["is_sharpe"].rank().corr(sensitivity["oos_sharpe"].rank()))
        if varied else np.nan,
    }


def stress_tests(wf: WalkForwardResult, data: MarketData, settings: BacktestSettings) -> dict:
    """Re-simulate the walk-forward book under harsher cost and execution assumptions."""
    periods, rf = settings.periods_per_year, settings.risk_free_rate
    base = sharpe_ratio(wf.returns, periods, rf)
    out = {"base": base}
    for k in (2.0, 3.0):
        r = settings.run(wf.targets, data, costs=settings.costs.scaled(k)).returns.loc[wf.start:]
        out[f"costs_x{k:g}"] = sharpe_ratio(r, periods, rf)
    if settings.execution != "next_close":
        r = settings.run(wf.targets, data, execution="next_close").returns.loc[wf.start:]
        out["delay_1_session"] = sharpe_ratio(r, periods, rf)
    if settings.execution != "same_close":
        r = settings.run(wf.targets, data, execution="same_close").returns.loc[wf.start:]
        out["same_close_optimistic"] = sharpe_ratio(r, periods, rf)
    out["cost_resilience"] = (out["costs_x2"] / base) if base > 0 else 0.0
    return out


def market_regimes(benchmark: pd.Series, periods: int = 252) -> pd.DataFrame:
    """Label each date with a trend regime and a volatility regime (both causal)."""
    sma = benchmark.rolling(200, min_periods=150).mean()
    slope = sma.diff(20)
    trend = pd.Series("sideways", index=benchmark.index)
    trend[(benchmark > sma) & (slope > 0)] = "bull"
    trend[(benchmark < sma) & (slope < 0)] = "bear"
    trend[sma.isna()] = "n/a"
    vol = benchmark.pct_change(fill_method=None).rolling(21).std() * math.sqrt(periods)
    pct = vol.rolling(756, min_periods=252).rank(pct=True)
    vreg = pd.Series("normal vol", index=benchmark.index)
    vreg[pct >= 0.67] = "high vol"
    vreg[pct <= 0.33] = "low vol"
    vreg[pct.isna()] = "n/a"
    return pd.DataFrame({"trend": trend, "vol": vreg})


def regime_breakdown(r: pd.Series, bench_r: pd.Series, regimes: pd.DataFrame, periods: int, rf: float) -> pd.DataFrame:
    rows = []
    reg = regimes.reindex(r.index)
    for col in ("trend", "vol"):
        for name, idx in reg.groupby(col).groups.items():
            if name == "n/a" or len(idx) < 40:
                continue
            rs, bs = r.loc[idx], bench_r.reindex(idx).fillna(0.0)
            rows.append({
                "dimension": col, "regime": name, "share_of_time": len(idx) / len(r),
                "ann_return": float(rs.mean() * periods), "sharpe": sharpe_ratio(rs, periods, rf),
                "benchmark_ann_return": float(bs.mean() * periods),
                "benchmark_sharpe": sharpe_ratio(bs, periods, rf),
            })
    return pd.DataFrame(rows)


def drawdown_episodes(bench_r: pd.Series, top: int = 3, min_depth: float = 0.08) -> list[tuple]:
    """Largest peak-to-trough benchmark drawdowns (non-overlapping) as (peak, trough, depth)."""
    eq = (1 + bench_r).cumprod()
    dd = eq / eq.cummax() - 1
    episodes = []
    in_dd = dd < 0
    groups = (~in_dd).cumsum()
    for _, seg in dd[in_dd].groupby(groups[in_dd]):
        trough = seg.idxmin()
        depth = -seg.min()
        peak_candidates = dd.loc[:seg.index[0]]
        peak = peak_candidates.index[-2] if len(peak_candidates) > 1 else seg.index[0]
        if depth >= min_depth:
            episodes.append((peak, trough, float(depth)))
    episodes.sort(key=lambda e: -e[2])
    return episodes[:top]


NAMED_CRISES = {
    "GFC 2007-09": ("2007-10-09", "2009-03-09"),
    "Euro debt 2011": ("2011-04-29", "2011-10-03"),
    "China deval 2015": ("2015-07-20", "2016-02-11"),
    "Q4 2018 selloff": ("2018-09-20", "2018-12-24"),
    "COVID crash 2020": ("2020-02-19", "2020-03-23"),
    "Inflation bear 2022": ("2022-01-03", "2022-10-12"),
}


def crisis_performance(r: pd.Series, bench_r: pd.Series, include_named: bool) -> pd.DataFrame:
    rows = []
    for peak, trough, depth in drawdown_episodes(bench_r.loc[r.index[0]:]):
        rows.append({"episode": f"Benchmark drawdown {peak.date()} -> {trough.date()}",
                     "strategy": float((1 + r.loc[peak:trough]).prod() - 1),
                     "benchmark": float((1 + bench_r.loc[peak:trough]).prod() - 1)})
    if include_named:
        for name, (a, b) in NAMED_CRISES.items():
            if pd.Timestamp(a) >= r.index[0] and pd.Timestamp(b) <= r.index[-1]:
                rows.append({"episode": name, "strategy": float((1 + r.loc[a:b]).prod() - 1),
                             "benchmark": float((1 + bench_r.loc[a:b]).prod() - 1)})
    return pd.DataFrame(rows, columns=["episode", "strategy", "benchmark"])


def concentration(wf: WalkForwardResult, data: MarketData, settings: BacktestSettings) -> dict:
    """How much of the P&L comes from one asset, and what happens without each asset."""
    contrib = wf.result.contributions.loc[wf.start:].sum()
    positive = contrib[contrib > 0]
    share = float(positive.max() / positive.sum()) if len(positive) and positive.sum() > 0 else float("nan")
    out = {"pnl_by_asset": contrib.sort_values(ascending=False), "top_asset": positive.idxmax() if len(positive) else None,
           "top_asset_share": share, "leave_one_out_min_sharpe": float("nan"), "leave_one_out_worst_asset": None}
    if len(data.assets) < 2:
        return out
    periods, rf = settings.periods_per_year, settings.risk_free_rate
    worst, worst_asset = float("inf"), None
    ranked = contrib.abs().sort_values(ascending=False).index[: min(len(data.assets), 6)]
    for a in ranked:
        t = wf.targets.copy()
        t[a] = 0.0
        sh = sharpe_ratio(settings.run(t, data).returns.loc[wf.start:], periods, rf)
        if sh < worst:
            worst, worst_asset = sh, a
    out.update({"leave_one_out_min_sharpe": worst, "leave_one_out_worst_asset": worst_asset})
    return out


@dataclass
class StrategyValidation:
    """Everything learned about one strategy (its whole parameter grid)."""

    strategy_cls: type[Strategy]
    runs: list[GridRun]
    lookahead: dict
    is_choice: int
    sensitivity: pd.DataFrame
    robustness: dict
    walk_forward: WalkForwardResult
    stress: dict
    regimes: pd.DataFrame
    crises: pd.DataFrame
    concentration: dict
    metrics_is: dict = field(default_factory=dict)
    metrics_oos: dict = field(default_factory=dict)
    metrics_wf: dict = field(default_factory=dict)
    significance: dict = field(default_factory=dict)
    verdict: Optional[object] = None
    iteration_note: str = ""
    deployed_params: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.strategy_cls.name
