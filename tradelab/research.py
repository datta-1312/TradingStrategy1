"""The research pipeline: market analysis -> opportunity scan -> ideation ->
implementation -> backtesting -> evaluation -> iteration -> final selection.
"""
from __future__ import annotations

import math
import time
import warnings
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from .backtest import BacktestResult, buy_and_hold
from .config import ResearchConfig
from .costs import CostModel, get_cost_model
from .data import MarketData, load_market_data
from .evaluation import evaluate, rank_strategies, select_final
from .market import fetch_macro, market_overview, opportunity_scan
from .metrics import bootstrap_sharpe_ci, deflated_sharpe, performance_summary, probabilistic_sharpe
from .strategies import STRATEGIES, Strategy, make_overlay
from .validation import (BacktestSettings, StrategyValidation, concentration, crisis_performance,
                         lookahead_check, market_regimes, parameter_sensitivity, regime_breakdown,
                         robustness_score, run_grid, select_params, stress_tests, walk_forward,
                         walk_forward_windows)


@dataclass
class ResearchResults:
    config: ResearchConfig
    data: MarketData
    costs: CostModel
    overview: dict
    scan: pd.DataFrame
    macro: Optional[pd.DataFrame]
    validations: list[StrategyValidation]
    skipped: list[tuple[str, str]]
    iteration_log: list[str]
    ranking: pd.DataFrame
    selected: list[StrategyValidation]
    benchmark_metrics: dict
    baseline_metrics: dict
    blend: Optional[dict]
    windows: dict
    n_trials: int
    sr_std: float
    notes: list[str] = field(default_factory=list)
    elapsed: float = 0.0

    def validation(self, name: str) -> StrategyValidation:
        return next(v for v in self.validations if v.name == name)


class ResearchPipeline:
    def __init__(self, config: ResearchConfig, data: Optional[MarketData] = None,
                 log: Callable[[str], None] = print):
        self.config = config
        self.data = data
        self.log = log
        self.costs = get_cost_model(config.cost_preset)
        self.settings = BacktestSettings(
            costs=self.costs, execution=config.execution, capital=config.capital,
            risk_free_rate=config.risk_free_rate, periods_per_year=config.periods_per_year,
        )
        self.notes: list[str] = list(config.notes)

    # ------------------------------------------------------------------ helpers
    @property
    def P(self) -> int:
        return self.config.periods_per_year

    @property
    def rf(self) -> float:
        return self.config.risk_free_rate

    def _summary(self, r: pd.Series, res: Optional[BacktestResult] = None) -> dict:
        bench = self.data.benchmark_returns().reindex(r.index)
        if res is None:
            return performance_summary(r, bench, self.P, self.rf)
        return performance_summary(r, bench, self.P, self.rf, trades=res.trades, turnover=res.turnover,
                                   exposure=res.weights, cost=res.cost)

    # ------------------------------------------------------------------ steps
    def load(self) -> MarketData:
        c = self.config
        if self.data is None:
            self.log(f"[1/8] Loading {len(c.universe)} assets + benchmark {c.benchmark} from {c.source} ...")
            self.data = load_market_data(c.universe, c.benchmark, c.start, c.end, c.source, c.csv_dir,
                                         c.cache_dir, seed=c.seed)
        if self.data.source == "synthetic":
            self.notes.insert(0, "SYNTHETIC DATA: results demonstrate the pipeline only and are NOT evidence "
                                 "about any real market.")
        return self.data

    def plan_windows(self) -> dict:
        idx = self.data.index
        warmup = min(300, len(idx) // 4)
        eval_start = idx[warmup]
        split = idx[warmup + int((len(idx) - warmup) * (1 - self.config.oos_fraction))]
        years = (idx[-1] - eval_start).days / 365.25
        train, test = self.config.wf_train_years, self.config.wf_test_years
        if years < train + 2 * test:
            train = max(1.0, round(years * 0.4, 1))
            test = max(0.25, round(years * 0.15, 2))
            self.notes.append(f"History too short for the configured walk-forward; using {train}y train / "
                              f"{test}y test windows.")
        wins = walk_forward_windows(idx, eval_start, train, test, self.config.wf_anchored)
        if not wins:
            raise ValueError("Not enough history for walk-forward validation")
        return {"eval_start": eval_start, "split": split, "wf": wins, "train_years": train, "test_years": test}

    def candidates(self) -> tuple[list[type[Strategy]], list[tuple[str, str]]]:
        names = self.config.strategies or list(STRATEGIES)
        chosen, skipped = [], []
        for n in names:
            cls = STRATEGIES[n]
            ok, why = cls.applicable(self.data)
            (chosen.append(cls) if ok else skipped.append((n, why)))
        return chosen, skipped

    def validate(self, cls: type[Strategy], windows: dict) -> StrategyValidation:
        data, s, P, rf = self.data, self.settings, self.P, self.rf
        t0 = time.time()
        la = lookahead_check(cls(), data)
        runs = run_grid(cls, data, s, self.config.max_grid, self.config.seed)
        es, split = windows["eval_start"], windows["split"]
        is_choice = select_params(runs, cls.param_grid, es, split, P, rf)
        sens = parameter_sensitivity(runs, es, split, P, rf)
        wf = walk_forward(runs, cls.param_grid, data, s, windows["wf"], cls.name)
        stress = stress_tests(wf, data, s)
        bench_r = data.benchmark_returns()
        regimes = regime_breakdown(wf.returns, bench_r, self._regimes, P, rf)
        crises = crisis_performance(wf.returns, bench_r, include_named=data.source != "synthetic")
        conc = concentration(wf, data, s)
        chosen = runs[is_choice]
        v = StrategyValidation(
            strategy_cls=cls, runs=runs, lookahead=la, is_choice=is_choice, sensitivity=sens,
            robustness=robustness_score(sens), walk_forward=wf, stress=stress, regimes=regimes,
            crises=crises, concentration=conc,
            metrics_is=self._summary(chosen.result.returns.loc[es:split], chosen.result),
            metrics_oos=self._summary(chosen.result.returns.loc[split:], chosen.result),
            metrics_wf=self._summary(wf.returns, wf.result),
            deployed_params=wf.windows[-1]["params"],
        )
        self.log(f"      {cls.name:<28} grid={len(runs):>2}  IS Sharpe {v.metrics_is.get('sharpe', 0):5.2f}  "
                 f"WF Sharpe {v.metrics_wf.get('sharpe', 0):5.2f}  ({time.time() - t0:.1f}s)")
        return v

    def iterate(self, validations: list[StrategyValidation], windows: dict) -> tuple[list, list[str]]:
        """Step 7: try risk overlays on strategies that show an edge but fail on risk grounds."""
        log, new = [], []
        risk_checks = {"Max drawdown", "Sharpe minus benchmark Sharpe", "Positive calendar years",
                       "Walk-forward Sharpe"}
        for v in validations:
            fails = {c.name for c in v.verdict.failures}
            sh = v.metrics_wf.get("sharpe", 0.0)
            if v.verdict.status == "VALIDATED":
                log.append(f"{v.name}: kept as is (validated).")
                continue
            if sh <= 0.2 or not (fails & risk_checks):
                reason = "no walk-forward edge to repair" if sh <= 0.2 else "failures are not risk-related"
                log.append(f"{v.name}: no overlay attempted ({reason}).")
                continue
            for kind in ("regime", "voltarget"):
                ocls = make_overlay(v.strategy_cls, kind)
                ov = self.validate(ocls, windows)
                ov.iteration_note = f"Iteration of {v.name} (failed: {', '.join(sorted(fails & risk_checks))})."
                new.append(ov)
                log.append(f"{v.name}: tried {kind} overlay -> WF Sharpe {ov.metrics_wf.get('sharpe', 0):.2f}, "
                           f"max DD {ov.metrics_wf.get('max_drawdown', 0):.0%} "
                           f"(was {sh:.2f}, {v.metrics_wf.get('max_drawdown', 0):.0%}).")
        return new, log

    def significance(self, validations: list[StrategyValidation]) -> tuple[int, float]:
        """Deflated Sharpe: haircut each walk-forward Sharpe for the total number of trials run."""
        es = self._windows["eval_start"]
        all_sr = np.array([
            r.result.returns.loc[es:].mean() / r.result.returns.loc[es:].std()
            for v in validations for r in v.runs if r.result.returns.loc[es:].std() > 0
        ])
        n_trials = int(sum(len(v.runs) for v in validations))
        sr_std = float(np.std(all_sr, ddof=1)) if len(all_sr) > 1 else 0.0
        for v in validations:
            r = v.walk_forward.returns
            lo, hi = bootstrap_sharpe_ci(r, self.P, n_boot=500, seed=self.config.seed)
            v.significance = {
                "psr": probabilistic_sharpe(r, 0.0, self.P),
                "dsr": deflated_sharpe(r, n_trials, sr_std, self.P),
                "sharpe_ci_low": lo, "sharpe_ci_high": hi,
                "t_stat": float(r.mean() / r.std() * math.sqrt(len(r))) if r.std() > 0 else 0.0,
            }
        return n_trials, sr_std

    def blend(self, selected: list[StrategyValidation]) -> Optional[dict]:
        """Equal-risk blend of the selected strategies' walk-forward returns (monthly rebalanced)."""
        if len(selected) < 2:
            return None
        rets = pd.concat({v.name: v.walk_forward.returns for v in selected}, axis=1).dropna()
        vol = rets.rolling(63, min_periods=21).std().shift(1)
        w = (1 / vol).div((1 / vol).sum(axis=1), axis=0)
        month = rets.index.to_period("M")
        w = w.groupby(month).transform("first").fillna(1 / len(selected))
        r = (w * rets).sum(axis=1)
        return {"returns": r, "metrics": self._summary(r), "correlation": rets.corr(),
                "components": [v.name for v in selected]}

    # ------------------------------------------------------------------ run
    def run(self) -> ResearchResults:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            return self._run()

    def _run(self) -> ResearchResults:
        t_start = time.time()
        c = self.config
        self.load()
        data = self.data
        self._regimes = market_regimes(data.benchmark, self.P)

        self.log("[2/8] Market overview & opportunity scan ...")
        overview = market_overview(data, self.P)
        scan = opportunity_scan(data, self.P)
        macro = fetch_macro(c.start, c.cache_dir) if data.source == "yahoo" else None

        self.log("[3/8] Strategy ideation ...")
        cands, skipped = self.candidates()
        for n, why in skipped:
            self.log(f"      skip {n}: {why}")

        self._windows = windows = self.plan_windows()
        self.log(f"[4-6/8] Implement, backtest and validate {len(cands)} strategies "
                 f"({len(windows['wf'])} walk-forward windows) ...")
        validations = [self.validate(cls, windows) for cls in cands]

        es = windows["eval_start"]
        wf_start = windows["wf"][0]["test_start"]
        bench_r = data.benchmark_returns()
        bench_metrics = {
            "full": performance_summary(bench_r.loc[es:], None, self.P, self.rf),
            "wf": performance_summary(bench_r.loc[wf_start:], None, self.P, self.rf),
        }
        bh = buy_and_hold(data, self.costs, c.execution, capital=c.capital, risk_free_rate=c.risk_free_rate,
                          periods_per_year=self.P)
        baseline = {"wf": self._summary(bh.returns.loc[wf_start:], bh), "returns": bh.returns}

        n_trials, sr_std = self.significance(validations)
        for v in validations:
            v.verdict = evaluate(v, c.criteria, data.has_volume)

        iteration_log: list[str] = []
        if c.iterate:
            self.log("[7/8] Iteration: repairing weak strategies with risk overlays ...")
            extra, iteration_log = self.iterate(validations, windows)
            if extra:
                validations += extra
                n_trials, sr_std = self.significance(validations)
                for v in validations:
                    v.verdict = evaluate(v, c.criteria, data.has_volume)

        self.log("[8/8] Ranking and final selection ...")
        ranking = rank_strategies(validations)
        selected = select_final(validations, ranking, c.top_n)
        blend = self.blend(selected)
        return ResearchResults(
            config=c, data=data, costs=self.costs, overview=overview, scan=scan, macro=macro,
            validations=validations, skipped=skipped, iteration_log=iteration_log, ranking=ranking,
            selected=selected, benchmark_metrics=bench_metrics, baseline_metrics=baseline, blend=blend,
            windows=windows, n_trials=n_trials, sr_std=sr_std, notes=self.notes,
            elapsed=time.time() - t_start,
        )
