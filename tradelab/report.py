"""Report writer: Markdown report + charts + CSV/JSON artefacts.

Section order follows the research brief's <output_format>:
1 Executive Summary, 2 Market Analysis, 3 Strategy Details, 4 Backtest Results,
5 Comparison & Ranking, 6 Final Recommendations, 7 Deployment Considerations,
8 What Could Go Wrong, then appendices (final check, methodology, logs).
"""
from __future__ import annotations

import inspect
import json
import math
import os
from datetime import date
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from . import charts
from .metrics import yearly_returns
from .research import ResearchResults
from .strategies import overlays as overlays_module
from .validation import StrategyValidation

# ---------------------------------------------------------------------- formatting


def _finite(x) -> bool:
    try:
        return x is not None and math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def pct(x, d: int = 1) -> str:
    return f"{float(x) * 100:.{d}f}%" if _finite(x) else "n/a"


def num(x, d: int = 2) -> str:
    if isinstance(x, (float, np.floating)) and np.isinf(x):
        return "inf"
    return f"{float(x):.{d}f}" if _finite(x) else "n/a"


def md_table(df: pd.DataFrame, formats: Optional[dict] = None, index: bool = True) -> str:
    if df is None or len(df) == 0:
        return "_No data._\n"
    formats = formats or {}
    d = df.reset_index() if index else df.copy()
    cols = [str(c) for c in d.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for _, row in d.iterrows():
        cells = []
        for c, v in zip(d.columns, row):
            f = formats.get(c)
            if f is not None:
                cells.append(f(v))
            elif isinstance(v, (float, np.floating)):
                cells.append(num(v))
            else:
                cells.append(str(v).replace("|", "/"))
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


STATUS_BADGE = {"VALIDATED": "✅ VALIDATED", "PROMISING": "🟡 PROMISING", "REJECTED": "❌ REJECTED"}

METRIC_ROWS = [
    ("total_return", "Total return", pct), ("cagr", "CAGR", pct), ("ann_vol", "Volatility", pct),
    ("sharpe", "Sharpe", num), ("sortino", "Sortino", num), ("max_drawdown", "Max drawdown", pct),
    ("calmar", "Calmar", num), ("win_rate", "Win rate (trades)", pct), ("profit_factor", "Profit factor", num),
    ("n_trades", "Trades", lambda x: f"{int(x)}" if _finite(x) else "n/a"),
    ("avg_trade_bars", "Avg holding (bars)", lambda x: num(x, 1)),
    ("annual_turnover", "Turnover / yr", lambda x: f"{num(x, 1)}x"),
    ("annual_cost_drag", "Cost drag / yr", pct), ("time_in_market", "Time in market", pct),
    ("beta", "Beta", num), ("positive_years", "Positive years", pct), ("worst_year", "Worst year", pct),
    ("cvar_95", "CVaR 95% (daily)", pct),
]


def _code_link(cls, report_dir: Path) -> str:
    target = getattr(cls, "base", cls)
    path = Path(inspect.getfile(target))
    rel = os.path.relpath(path, report_dir)
    extra = ""
    if target is not cls:
        extra = f" + overlay in [`overlays.py`]({os.path.relpath(inspect.getfile(overlays_module), report_dir)})"
    return f"[`{path.name}` → `{target.__name__}`]({rel}){extra}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _params(p: dict) -> str:
    return ", ".join(f"`{k}={v}`" for k, v in p.items())


# ---------------------------------------------------------------------- report


class ReportBuilder:
    def __init__(self, res: ResearchResults, out_dir: Optional[str] = None):
        self.res = res
        c = res.config
        stamp = date.today().isoformat()
        self.dir = Path(out_dir) if out_dir else Path(c.output_dir) / f"{c.name}_{stamp}"
        self.charts_dir = self.dir / "charts"
        self.results_dir = self.dir / "results"
        self.by_name = {v.name: v for v in res.validations}
        self.ranked = [self.by_name[n] for n in res.ranking["strategy"]] if len(res.ranking) else []
        # Strategies shown in charts and detailed tables (at most 4 so colours stay distinguishable).
        self.focus = (res.selected or self.ranked[:3])[:4]
        self.colours = charts.colour_map([v.name for v in self.focus])
        self.P = c.periods_per_year

    # -------------------------------------------------------------- artefacts
    def write(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.charts_dir.mkdir(exist_ok=True)
        self.results_dir.mkdir(exist_ok=True)
        self._save_results()
        imgs = self._charts()
        md = "\n".join([
            self._header(), self._executive(), self._market(imgs), self._details(), self._backtests(imgs),
            self._comparison(imgs), self._recommendations(), self._deployment(), self._risks(),
            self._final_check(), self._appendix(),
        ])
        path = self.dir / "REPORT.md"
        path.write_text(md, encoding="utf-8")
        return path

    def _save_results(self) -> None:
        r = self.res
        r.ranking.to_csv(self.results_dir / "ranking.csv", index=False)
        pd.DataFrame({v.name: v.metrics_wf for v in r.validations}).T.to_csv(self.results_dir / "walk_forward_metrics.csv")
        pd.concat({v.name: v.walk_forward.returns for v in r.validations}, axis=1).to_csv(
            self.results_dir / "walk_forward_returns.csv")
        for v in r.validations:
            v.sensitivity.to_csv(self.results_dir / f"sensitivity_{v.name.replace('+', '_')}.csv", index=False)
        if len(r.scan):
            r.scan.to_csv(self.results_dir / "opportunity_scan.csv")
        summary = {
            "config": r.config.to_dict(),
            "costs": r.costs.to_dict(),
            "data_source": r.data.source,
            "period": [str(r.data.index[0].date()), str(r.data.index[-1].date())],
            "n_trials": r.n_trials,
            "selected": [{"strategy": v.name, "status": v.verdict.status, "deployed_params": v.deployed_params,
                          "wf_metrics": {k: v.metrics_wf.get(k) for k in ("sharpe", "cagr", "max_drawdown")},
                          "dsr": v.significance.get("dsr")} for v in r.selected],
            "ranking": r.ranking.to_dict(orient="records"),
        }
        (self.results_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))

    def _charts(self) -> dict[str, str]:
        r, out = self.res, {}
        wf_start = r.windows["wf"][0]["test_start"]
        bench = r.data.benchmark_returns().loc[wf_start:]
        series = {v.name: v.walk_forward.returns for v in self.focus}
        if series:
            out["equity"] = charts.equity_chart(series, bench, r.data.benchmark_name, self.colours,
                                                self.charts_dir / "equity.png",
                                                "Walk-forward (out-of-sample) growth of 1, net of costs")
            out["drawdown"] = charts.drawdown_chart(series, bench, r.data.benchmark_name, self.colours,
                                                    self.charts_dir / "drawdown.png", "Walk-forward drawdowns")
        if len(r.ranking):
            out["sharpe"] = charts.sharpe_bars(r.ranking, r.benchmark_metrics["wf"].get("sharpe", 0.0),
                                               r.config.criteria.min_sharpe, self.charts_dir / "sharpe.png")
            out["sensitivity"] = charts.sensitivity_strip(
                {v.name: v.sensitivity for v in self.ranked}, {}, self.charts_dir / "sensitivity.png")
        yr = self._yearly_table()
        if len(yr):
            out["yearly"] = charts.heatmap(yr.T, self.charts_dir / "yearly.png", "Calendar-year returns (walk-forward)")
        if len(self.focus) >= 2:
            corr = pd.concat({v.name: v.walk_forward.returns for v in self.focus}, axis=1).corr()
            out["corr"] = charts.heatmap(corr, self.charts_dir / "correlation.png",
                                         "Correlation of walk-forward daily returns", fmt_pct=False, vmax=1.0)
        out["regime"] = charts.regime_chart(r.data.benchmark, r.overview["regime_history"], r.data.benchmark_name,
                                            self.charts_dir / "market_regime.png")
        return out

    def _yearly_table(self) -> pd.DataFrame:
        r = self.res
        if not self.focus:
            return pd.DataFrame()
        wf_start = r.windows["wf"][0]["test_start"]
        cols = {v.name: yearly_returns(v.walk_forward.returns) for v in self.focus}
        cols[f"{r.data.benchmark_name} (benchmark)"] = yearly_returns(r.data.benchmark_returns().loc[wf_start:])
        return pd.DataFrame(cols)

    # -------------------------------------------------------------- sections
    def _header(self) -> str:
        r, c = self.res, self.res.config
        lines = [f"# Trading Strategy Research Report: `{c.name}`", ""]
        for n in r.notes:
            lines.append(f"> ⚠️ **{n}**" if "SYNTHETIC" in n or "SURVIVORSHIP" in n else f"> ℹ️ {n}")
            lines.append(">")
        if r.notes:
            lines.pop()
        lines += [
            "",
            "| Item | Value |", "|---|---|",
            f"| Universe | {', '.join(r.data.assets)} ({_plural(len(r.data.assets), 'asset')}) |",
            f"| Benchmark | {r.data.benchmark_name} |",
            f"| Data | {r.data.source}, {r.data.index[0].date()} → {r.data.index[-1].date()} ({len(r.data.index):,} sessions) |",
            f"| Costs | {r.costs.describe()} |",
            f"| Execution | `{c.execution}` · capital {c.capital:,.0f} · risk-free {pct(c.risk_free_rate)} |",
            f"| Validation | IS/OOS split at {r.windows['split'].date()}; walk-forward {r.windows['train_years']}y train / "
            f"{r.windows['test_years']}y test ({'anchored' if c.wf_anchored else 'rolling'}), {len(r.windows['wf'])} windows |",
            f"| Trials | {r.n_trials} parameter configurations across {len(r.validations)} strategy variants |",
            f"| Generated | {date.today().isoformat()} in {r.elapsed:.0f}s by `tradelab` |",
            "",
        ]
        return "\n".join(lines)

    def _executive(self) -> str:
        r = self.res
        counts = r.ranking["status"].value_counts() if len(r.ranking) else pd.Series(dtype=int)
        bm = r.benchmark_metrics["wf"]
        out = ["## 1. Executive Summary", ""]
        out.append(
            f"**{len(r.validations)} strategy variants** ({len([v for v in r.validations if '+' not in v.name])} base "
            f"strategies + {len([v for v in r.validations if '+' in v.name])} iteration variants) were researched "
            f"on {_plural(len(r.data.assets), 'asset')}, testing **{r.n_trials} parameter configurations** in total. "
            f"Result: **{counts.get('VALIDATED', 0)} validated**, **{counts.get('PROMISING', 0)} promising**, "
            f"**{counts.get('REJECTED', 0)} rejected**.")
        out.append("")
        out.append(f"Benchmark ({r.data.benchmark_name}) over the same walk-forward period: CAGR {pct(bm.get('cagr'))}, "
                   f"Sharpe {num(bm.get('sharpe'))}, max drawdown {pct(bm.get('max_drawdown'))}.")
        out.append("")
        if r.selected:
            out.append("**Top strategies (walk-forward, out-of-sample, net of costs):**")
            out.append("")
            for i, v in enumerate(r.selected, 1):
                m = v.metrics_wf
                out.append(f"{i}. **{v.name}** ({STATUS_BADGE[v.verdict.status]}): {v.strategy_cls.family}. "
                           f"Sharpe {num(m.get('sharpe'))} vs {num(m.get('benchmark_sharpe'))}, "
                           f"CAGR {pct(m.get('cagr'))}, max DD {pct(m.get('max_drawdown'))}, "
                           f"deflated Sharpe {num(v.significance.get('dsr'))}.")
            if r.blend:
                bmx = r.blend["metrics"]
                out += ["", f"An equal-risk blend of the selected strategies returned Sharpe {num(bmx.get('sharpe'))}, "
                            f"CAGR {pct(bmx.get('cagr'))}, max DD {pct(bmx.get('max_drawdown'))} out of sample."]
        else:
            out.append("**No strategy cleared the quality bar.** Out of sample and after realistic costs, none of the "
                       "candidates beat the benchmark on a risk-adjusted basis with acceptable drawdown and robustness. "
                       "**Recommendation: do not deploy any of these strategies on this universe.** Holding the "
                       "benchmark is the better-evidenced choice. See §6 for next steps.")
        out += ["", "**Key findings**", ""] + [f"- {f}" for f in self._findings()] + [""]
        return "\n".join(out)

    def _findings(self) -> list[str]:
        r, f = self.res, []
        vs = r.validations
        if not vs:
            return ["No applicable strategies."]
        best = max(vs, key=lambda v: v.metrics_wf.get("sharpe", -9))
        f.append(f"Best out-of-sample Sharpe: **{best.name}** at {num(best.metrics_wf.get('sharpe'))} "
                 f"(benchmark {num(best.metrics_wf.get('benchmark_sharpe'))}).")
        decays = [(v.metrics_wf.get("sharpe", 0) / v.metrics_is["sharpe"]) for v in vs if v.metrics_is.get("sharpe", 0) > 0.2]
        if decays:
            f.append(f"Median walk-forward Sharpe was **{np.median(decays):.0%} of the in-sample Sharpe**, which shows "
                     "how much a naive in-sample backtest overstates the edge.")
        cost_fail = [v.name for v in vs if v.stress.get("cost_resilience", 0) < r.config.criteria.min_cost_resilience
                     and v.metrics_wf.get("sharpe", 0) > 0]
        if cost_fail:
            f.append(f"Cost-fragile (Sharpe falls by more than half at 2x costs): {', '.join(cost_fail)}.")
        delay_fail = [v.name for v in vs if v.metrics_wf.get("sharpe", 0) > 0.3
                      and v.stress.get("delay_1_session", 0) < 0.5 * v.metrics_wf.get("sharpe", 0)]
        if delay_fail:
            f.append(f"Execution-sensitive (edge halves with a one-session delay): {', '.join(delay_fail)}.")
        sig = [v.name for v in vs if v.significance.get("dsr", 0) >= r.config.criteria.min_deflated_sharpe]
        f.append(f"After a multiple-testing haircut for {r.n_trials} trials, {len(sig)} variant(s) have deflated "
                 f"Sharpe ≥ {r.config.criteria.min_deflated_sharpe}" + (f": {', '.join(sig)}." if sig else "."))
        la = [v.name for v in vs if not v.lookahead["passed"]]
        f.append("All strategies passed the look-ahead truncation test." if not la
                 else f"LOOK-AHEAD DETECTED in: {', '.join(la)}. These results are invalid.")
        facts = r.overview["facts"]
        f.append(f"Current market regime ({facts['as_of']}): **{facts['trend_regime']}** trend, "
                 f"**{facts['vol_regime']}** (21-day vol {pct(facts['realised_vol_21d'])}).")
        return f

    def _market(self, imgs: dict) -> str:
        r = self.res
        facts = r.overview["facts"]
        out = ["## 2. Market Analysis", "", "### 2.1 Current regime: facts", ""]
        rows = [
            ("As of", facts["as_of"]), ("Trend regime", facts["trend_regime"]), ("Volatility regime", facts["vol_regime"]),
            ("Return 1m / 3m / 6m / 12m", " / ".join(pct(facts[k]) for k in ("return_1m", "return_3m", "return_6m", "return_12m"))),
            ("Distance from 200-day SMA", pct(facts["distance_from_200d_sma"])),
            ("50-day SMA above 200-day", "yes" if facts["sma50_above_sma200"] else "no"),
            ("Drawdown from high", pct(facts["drawdown_from_high"])),
            ("Realised vol 21d (3y median)", f"{pct(facts['realised_vol_21d'])} ({pct(facts['realised_vol_3y_median'])})"),
            ("Vol percentile (3y)", pct(facts["vol_percentile_3y"], 0)),
        ]
        if "breadth_above_200d" in facts:
            rows += [
                ("Breadth: above 200d / 50d SMA", f"{pct(facts['breadth_above_200d'], 0)} / {pct(facts['breadth_above_50d'], 0)}"),
                ("Avg pairwise correlation 63d (3y pct)", f"{num(facts['avg_pairwise_corr_63d'])} ({pct(facts['corr_percentile_3y'], 0)})"),
                ("Cross-sectional dispersion (3m)", pct(facts["dispersion_3m"])),
            ]
        out += ["| Metric | Value |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows] + [""]
        if "regime" in imgs:
            out += [f"![Market regime](charts/{imgs['regime']})", ""]
        out += ["### 2.2 Interpretation (heuristic, not evidence)", ""]
        out += [f"- {s}" for s in r.overview["interpretation"]] + [""]
        out += ["### 2.3 Opportunity scan", ""]
        if len(r.scan):
            fm = {c: pct for c in ("ret_1m", "ret_3m", "ret_6m", "ret_12m", "rel_strength_6m", "vol_3m", "dist_52w_high")}
            fm.update({"rsi14": lambda x: num(x, 0), "z_1m_move": num, "volume_trend": num})
            out.append(md_table(r.scan, fm))
            flagged = r.scan[r.scan["flags"] != ""]
            if len(flagged):
                out += ["Flagged assets:", ""] + [f"- **{a}**: {flags}" for a, flags in flagged["flags"].items()] + [""]
        else:
            out.append("_Not enough history for the scan._\n")
        out += ["### 2.4 Macro context", ""]
        if r.macro is not None and len(r.macro):
            out.append(md_table(r.macro, {"last": num, "change_3m": pct, "change_12m": pct}, index=False))
        else:
            out.append("_Macro series not available for this data source. Rates, the dollar, oil and VIX are fetched "
                       "automatically only with `--source yahoo`. Fundamentals, options flow and news are **not** "
                       "used by this framework, so treat them as unassessed._\n")
        out += ["### 2.5 Data quality", ""]
        q = r.data.quality.copy()
        if len(q):
            out.append(md_table(q, {"max_abs_daily_move": pct}))
        return "\n".join(out)

    def _details(self) -> str:
        r = self.res
        out = ["## 3. Strategy Details", ""]
        if r.skipped:
            out += ["Strategies not applicable to this universe: " +
                    "; ".join(f"`{n}` ({why})" for n, why in r.skipped) + ".", ""]
        for i, v in enumerate(self.ranked, 1):
            cls = v.strategy_cls
            out += [
                f"### 3.{i} {v.name}: {STATUS_BADGE[v.verdict.status]}",
                "",
                f"*{cls.family}*" + (f". {v.iteration_note.strip()}" if v.iteration_note else ""),
                "",
                f"- **Thesis / why the edge might exist:** {cls.thesis}",
                f"- **Rules:** {cls.rules}",
                f"- **Position sizing:** {cls.sizing}",
                f"- **Rebalance:** {cls.rebalance}",
                f"- **Universe:** {', '.join(r.data.assets)} ({'long-only' if cls.long_only else 'long/short'})",
                "- **Parameter grid:** " + "; ".join(f"`{k}` ∈ {vals}" for k, vals in cls.param_grid.items()) +
                f" ({len(v.runs)} configurations)",
                f"- **In-sample choice:** {_params(v.runs[v.is_choice].params)}",
                f"- **Deployed (latest walk-forward window):** {_params(v.deployed_params)}",
                f"- **Invalidation:** {cls.invalidation}",
                f"- **References:** {'; '.join(cls.references)}",
                f"- **Code:** {_code_link(cls, self.dir)}",
                "",
            ]
        return "\n".join(out)

    def _metric_table(self, vs: list[StrategyValidation]) -> str:
        r = self.res
        cols = {v.name: v.metrics_wf for v in vs}
        cols[f"{r.data.benchmark_name} (benchmark)"] = r.benchmark_metrics["wf"]
        cols["Equal-weight buy & hold"] = r.baseline_metrics["wf"]
        rows = []
        for key, label, f in METRIC_ROWS:
            rows.append([label] + [f(cols[c].get(key)) if cols[c].get(key) is not None else "n/a" for c in cols])
        head = "| Metric | " + " | ".join(cols) + " |"
        return "\n".join([head, "|" + "---|" * (len(cols) + 1)] + ["| " + " | ".join(r_) + " |" for r_ in rows]) + "\n"

    def _backtests(self, imgs: dict) -> str:
        r = self.res
        w = r.windows
        out = ["## 4. Backtest Results", "", "### 4.1 How to read these numbers", "",
               f"- **Walk-forward (primary evidence):** parameters are re-chosen every {w['test_years']}y using only the "
               f"previous {w['train_years']}y, then traded on unseen data. The stitched test periods "
               f"({w['wf'][0]['test_start'].date()} → {r.data.index[-1].date()}) are the out-of-sample record. "
               "Switching costs between parameter sets are charged.",
               "- Parameter choice favours stable *plateaus* in parameter space over isolated peaks.",
               f"- Signals use the close of day *t*; trades execute `{r.config.execution}`. Costs, impact, borrow and "
               "financing are included. Idle cash earns the configured risk-free rate.",
               "- Win rate and profit factor are measured per position spell (entry to exit, per asset).", ""]
        out += ["### 4.2 Walk-forward performance (top strategies)", "", self._metric_table(self.focus)]
        if "equity" in imgs:
            out += [f"![Equity curves](charts/{imgs['equity']})", "", f"![Drawdowns](charts/{imgs['drawdown']})", ""]
        out += ["### 4.3 All strategies: in-sample vs out-of-sample (overfitting check)", ""]
        rows = []
        for v in self.ranked:
            rows.append({"strategy": v.name, "IS Sharpe": v.metrics_is.get("sharpe"),
                         "OOS Sharpe (IS params)": v.metrics_oos.get("sharpe"),
                         "Walk-forward Sharpe": v.metrics_wf.get("sharpe"),
                         "WF CAGR": v.metrics_wf.get("cagr"), "WF max DD": v.metrics_wf.get("max_drawdown"),
                         "WF Sortino": v.metrics_wf.get("sortino"), "Win rate": v.metrics_wf.get("win_rate"),
                         "Profit factor": v.metrics_wf.get("profit_factor"),
                         "Turnover/yr": v.metrics_wf.get("annual_turnover")})
        out.append(md_table(pd.DataFrame(rows), {"WF CAGR": pct, "WF max DD": pct, "Win rate": pct,
                                                  "Turnover/yr": lambda x: f"{num(x, 1)}x"}, index=False))
        yr = self._yearly_table()
        if len(yr):
            out += ["### 4.4 Calendar-year returns (walk-forward)", ""]
            if "yearly" in imgs:
                out += [f"![Yearly returns](charts/{imgs['yearly']})", ""]
            out.append(md_table(yr, {c: pct for c in yr.columns}))
        out += ["### 4.5 Robustness checks", "", "**Parameter sensitivity:** is the result a plateau or a spike?", ""]
        if "sensitivity" in imgs:
            out += [f"![Parameter sensitivity](charts/{imgs['sensitivity']})", ""]
        rows = [{"strategy": v.name, **{k: v.robustness.get(k) for k in
                 ("grid_size", "best_sharpe", "median_sharpe", "median_to_best", "frac_positive", "is_oos_rank_corr")}}
                for v in self.ranked]
        out.append(md_table(pd.DataFrame(rows), {"grid_size": lambda x: str(int(x)), "frac_positive": pct}, index=False))
        out += ["**Stress tests** (walk-forward Sharpe under harsher assumptions):", ""]
        rows = [{"strategy": v.name, "base": v.stress.get("base"), "costs x2": v.stress.get("costs_x2"),
                 "costs x3": v.stress.get("costs_x3"), "+1 session delay": v.stress.get("delay_1_session"),
                 "same-close (optimistic)": v.stress.get("same_close_optimistic")} for v in self.ranked]
        out.append(md_table(pd.DataFrame(rows), index=False))
        out += ["**Regime dependence** (walk-forward, annualised return / Sharpe by benchmark regime):", ""]
        for v in self.focus:
            if len(v.regimes):
                out += [f"*{v.name}*", "", md_table(v.regimes, {"share_of_time": pct, "ann_return": pct,
                                                                 "benchmark_ann_return": pct}, index=False)]
        out += ["**Crisis and drawdown episodes:**", ""]
        for v in self.focus:
            if len(v.crises):
                out += [f"*{v.name}*", "", md_table(v.crises, {"strategy": pct, "benchmark": pct}, index=False)]
        out += ["**Asset concentration** (share of positive P&L from the single best asset; Sharpe with the most "
                "important asset removed):", ""]
        rows = [{"strategy": v.name, "top asset": v.concentration.get("top_asset"),
                 "top asset P&L share": v.concentration.get("top_asset_share"),
                 "leave-one-out min Sharpe": v.concentration.get("leave_one_out_min_sharpe"),
                 "most important asset": v.concentration.get("leave_one_out_worst_asset")} for v in self.ranked]
        out.append(md_table(pd.DataFrame(rows), {"top asset P&L share": pct}, index=False))
        out += ["### 4.6 Statistical significance", "",
                "Probabilistic Sharpe (PSR) is P(true Sharpe > 0). The deflated Sharpe (DSR) also discounts for the "
                f"best-of-{r.n_trials}-trials selection effect (cross-trial Sharpe dispersion "
                f"{num(r.sr_std * math.sqrt(self.P))} annualised). The 95% CI comes from a stationary block bootstrap.", ""]
        rows = [{"strategy": v.name, "WF Sharpe": v.metrics_wf.get("sharpe"),
                 "95% CI": f"[{num(v.significance.get('sharpe_ci_low'))}, {num(v.significance.get('sharpe_ci_high'))}]",
                 "t-stat": v.significance.get("t_stat"), "PSR": v.significance.get("psr"),
                 "DSR": v.significance.get("dsr")} for v in self.ranked]
        out.append(md_table(pd.DataFrame(rows), index=False))
        return "\n".join(out)

    def _comparison(self, imgs: dict) -> str:
        r = self.res
        out = ["## 5. Comparison & Ranking", "",
               "Ranking: verdict first (VALIDATED > PROMISING > REJECTED), then a 0-100 score weighting out-of-sample "
               "Sharpe (30), deflated Sharpe (15), parameter robustness (10), drawdown (10), cost resilience (10), "
               "excess Sharpe vs benchmark (10), yearly consistency (5), regime consistency (5) and diversification "
               "across assets (5).", ""]
        rk = r.ranking.copy()
        if len(rk):
            rk["status"] = rk["status"].map(STATUS_BADGE)
            out.append(md_table(rk, {"wf_cagr": pct, "wf_max_dd": pct, "annual_turnover": lambda x: f"{num(x, 1)}x",
                                     "score": lambda x: num(x, 1)}, index=False))
        if "sharpe" in imgs:
            out += [f"![Sharpe ranking](charts/{imgs['sharpe']})", ""]
        out += ["### Pros and cons", ""]
        for v in self.ranked:
            pros = [c.name for c in v.verdict.checks if c.passed is True and c.kind in ("hard", "evidence", "soft")]
            cons = [f"{c.name} ({c.shown} vs {c.threshold})"
                    for c in v.verdict.checks if c.passed is False]
            out += [f"**{v.name}** ({STATUS_BADGE[v.verdict.status]}, score {num(v.verdict.score, 1)})", "",
                    f"- ➕ {'; '.join(pros) if pros else 'none'}", f"- ➖ {'; '.join(cons) if cons else 'none'}", ""]
        if "corr" in imgs:
            out += ["### Diversification between the top strategies", "", f"![Correlation](charts/{imgs['corr']})", ""]
            corr = pd.concat({v.name: v.walk_forward.returns for v in self.focus}, axis=1).corr()
            out.append(md_table(corr))
        return "\n".join(out)

    def _recommendations(self) -> str:
        r = self.res
        out = ["## 6. Final Recommendations", ""]
        if not r.selected:
            out += [
                "**No strategy is recommended for deployment.** Every candidate failed at least one hard criterion "
                "out of sample. That is a valid research outcome: the brief says never to claim a strategy works "
                "without proper validation.",
                "",
                "Closest candidates and what blocked them:",
                "",
            ]
            for v in self.ranked[:3]:
                out.append(f"- **{v.name}** (score {num(v.verdict.score, 1)}): " + ("; ".join(v.verdict.reasons) or "n/a"))
            out += ["", "Suggested next steps:", "",
                    "1. Re-run on a broader or different universe (more assets add cross-sectional breadth and statistical power).",
                    "2. Extend history. Short samples cannot separate a Sharpe of 0.5 from zero.",
                    "3. Bring in data this framework does not use (fundamentals, options positioning, earnings events) "
                    "and only then design new hypotheses. Do not tune the rejected rules further, because that is data snooping.",
                    ""]
            return "\n".join(out)
        for i, v in enumerate(r.selected, 1):
            m = v.metrics_wf
            out += [f"### {i}. {v.name}: {STATUS_BADGE[v.verdict.status]}", "",
                    f"- **Why:** {v.strategy_cls.thesis}",
                    f"- **Evidence:** walk-forward Sharpe {num(m.get('sharpe'))} (benchmark {num(m.get('benchmark_sharpe'))}), "
                    f"CAGR {pct(m.get('cagr'))}, max DD {pct(m.get('max_drawdown'))}, {pct(m.get('positive_years'), 0)} "
                    f"positive years, DSR {num(v.significance.get('dsr'))}, Sharpe at 2x costs {num(v.stress.get('costs_x2'))}.",
                    f"- **Parameters to deploy:** {_params(v.deployed_params)}",
                    ("- **Caveat:** evidence is not conclusive (" + "; ".join(v.verdict.reasons) +
                     "). Paper-trade or deploy at reduced size." if v.verdict.status == "PROMISING" else
                     "- **Caveat:** passing validation is not a guarantee. See §8."),
                    ""]
        if r.blend:
            bm = r.blend["metrics"]
            out += ["### Combined allocation", "",
                    f"An equal-risk blend (inverse 63-day volatility, monthly rebalance) of {', '.join(r.blend['components'])} "
                    f"produced walk-forward Sharpe {num(bm.get('sharpe'))}, CAGR {pct(bm.get('cagr'))} and max drawdown "
                    f"{pct(bm.get('max_drawdown'))}. Diversifying across uncorrelated edges is usually more robust "
                    "than concentrating in the single best backtest.", ""]
        return "\n".join(out)

    def _deployment(self) -> str:
        r = self.res
        c = r.config
        out = ["## 7. Deployment Considerations", ""]
        subjects = r.selected or []
        if not subjects:
            out += ["Not applicable: nothing is recommended for deployment. The framework in §4 should be re-run "
                    "on any new idea before capital is committed.", ""]
        for v in subjects:
            m = v.metrics_wf
            part = float(v.walk_forward.result.participation.loc[v.walk_forward.start:].max())
            capacity = c.capital * c.criteria.max_adv_participation / part if part > 0 else float("inf")
            dd = m.get("max_drawdown", 0.2)
            out += [f"### {v.name}", "",
                    f"- **Sizing:** {v.strategy_cls.sizing}",
                    f"- **Schedule:** {v.strategy_cls.rebalance} Compute signals after the close and execute at the "
                    f"{'next open (opening auction)' if c.execution == 'next_open' else 'close auction'}.",
                    f"- **Expected trading load:** turnover {num(m.get('annual_turnover'), 1)}x/yr, cost drag "
                    f"{pct(m.get('annual_cost_drag'), 2)}/yr, average holding {num(m.get('avg_trade_bars'), 1)} sessions.",
                    f"- **Liquidity & capacity:** max {pct(part, 3)} of ADV traded at {c.capital:,.0f} capital. That "
                    f"implies roughly {capacity:,.0f} capital before hitting {pct(c.criteria.max_adv_participation, 0)} of ADV "
                    "(impact costs grow with the square root of size well before that)." if np.isfinite(capacity) else
                    "- **Liquidity & capacity:** no volume data, so capacity was not assessed.",
                    f"- **Risk budget:** walk-forward vol {pct(m.get('ann_vol'))}, worst day {pct(m.get('worst_day'))}, "
                    f"CVaR95 {pct(m.get('cvar_95'))}. Size the sleeve so that a {pct(1.5 * dd, 0)} drawdown "
                    "(1.5x the worst seen out of sample) is tolerable.",
                    f"- **Kill switch:** halt and review if live drawdown exceeds {pct(min(1.5 * dd, 0.5), 0)}, if rolling "
                    "12-month Sharpe drops below 0, or if realised slippage exceeds 2x the modelled cost.",
                    "- **Monitoring:** daily P&L vs model, weekly implementation shortfall vs assumed costs, monthly "
                    "regime/parameter review, and a quarterly re-run of this pipeline with the latest data.",
                    ""]
        out += ["**General rules:** paper-trade for at least 3 months before committing capital. Scale in gradually. "
                "Keep the parameter set frozen between scheduled reviews. Re-validate after any rule change, "
                "because every change is a new trial.", ""]
        return "\n".join(out)

    def _risks(self) -> str:
        r = self.res
        out = ["## 8. What Could Go Wrong", ""]
        for v in (r.selected or self.ranked[:3]):
            m = v.metrics_wf
            worst_crisis = v.crises.sort_values("strategy").iloc[0] if len(v.crises) else None
            out += [f"**{v.name}**", "",
                    f"- *Thesis invalidation:* {v.strategy_cls.invalidation}",
                    f"- *Tail risk:* worst day {pct(m.get('worst_day'))}, worst year {pct(m.get('worst_year'))}, "
                    f"longest drawdown {m.get('max_dd_duration_days', 'n/a')} days.",
                    (f"- *Worst stress episode:* {worst_crisis['episode']}: strategy {pct(worst_crisis['strategy'])} vs "
                     f"benchmark {pct(worst_crisis['benchmark'])}." if worst_crisis is not None else
                     "- *Worst stress episode:* no large benchmark drawdown in the test period, so crisis behaviour is untested."),
                    f"- *Execution:* Sharpe {num(v.stress.get('delay_1_session'))} with a one-session delay, "
                    f"{num(v.stress.get('costs_x3'))} at 3x costs.",
                    ""]
        out += ["**Across all strategies**", "",
                "- *Regime shift:* the walk-forward period may not contain the next regime (for example rate shocks, "
                "a liquidity crisis, or a structural change in market microstructure).",
                "- *Crowding & decay:* published anomalies weaken after publication and as capital crowds in. "
                "Expect live Sharpe below backtest Sharpe.",
                "- *Residual overfitting:* even with walk-forward and deflated Sharpe, the choice of strategy "
                "families, universe and grid was made by a researcher who knows market history.",
                "- *Correlation spikes:* strategies that look diversified can lose together in a crisis.",
                "- *Data risk:* adjusted prices can be revised, and bad ticks or survivorship bias (see notes above) "
                "inflate results.",
                "- *Operational:* missed rebalances, broker outages, fat-finger errors and tax treatment are not modelled.",
                ""]
        return "\n".join(out)

    def _final_check(self) -> str:
        r = self.res
        vs = self.ranked
        decays = [v.metrics_wf.get("sharpe", 0) / v.metrics_is["sharpe"] for v in vs if v.metrics_is.get("sharpe", 0) > 0.2]
        n_regime = sum(
            1 for v in r.selected
            if len(v.regimes) and (v.regimes.loc[v.regimes["dimension"] == "trend", "sharpe"] > 0).mean() >= 0.67
        )
        missing = ["fundamental data (earnings, valuation)", "options flow / implied volatility surface",
                   "news & event calendars", "intraday data (true open/close auction fills)"]
        if r.macro is None or not len(r.macro):
            missing.append("macro series")
        out = ["## Appendix A: Final check", "",
               f"- **What am I missing?** Unused inputs: {', '.join(missing)}. " + " ".join(n for n in r.notes if "SURVIVORSHIP" in n or "SYNTHETIC" in n),
               f"- **Are my assumptions weak?** The key assumptions are costs ({r.costs.name}), execution "
               f"(`{r.config.execution}`) and the stability of relationships in the data. §4.5 stress-tests the first two; "
               "the third is probed by walk-forward and regime splits.",
               f"- **Is the edge real or overfitting?** Median walk-forward/in-sample Sharpe ratio: "
               f"{(f'{np.median(decays):.0%}') if decays else 'n/a'}. Deflated Sharpe accounts for {r.n_trials} trials. "
               f"{sum(1 for v in vs if v.significance.get('dsr', 0) >= r.config.criteria.min_deflated_sharpe)} variant(s) "
               "clear the significance bar.",
               f"- **Have I considered different regimes?** Yes: bull/bear/sideways and low/normal/high volatility splits, "
               f"benchmark drawdown episodes, and calendar years. {n_regime} of {len(r.selected)} selected strategies are "
               "positive in at least 2/3 of trend regimes.",
               "- **What evidence would invalidate the thesis?** Listed per strategy in §3 (*Invalidation*) and §8.",
               "- **Are the results robust and realistic?** Every result is out of sample, net of costs, with no look-ahead "
               "(truncation-tested), stress-tested at 2-3x costs and with delayed execution.",
               "- **What would make me change my mind?** Live performance breaching the kill-switch levels in §7, "
               "quarterly re-runs flipping the verdict, or new data (longer history, other markets) contradicting the result.",
               ""]
        return "\n".join(out)

    def _appendix(self) -> str:
        r = self.res
        out = ["## Appendix B: Methodology, logs and configuration", "", "### Iteration log", ""]
        out += [f"- {line}" for line in r.iteration_log] or ["- Iteration disabled."]
        out += ["", "### Look-ahead truncation tests", "",
                "Each strategy's weights were recomputed on histories truncated at 45%, 70% and 90% of the sample. "
                "Any difference versus the full-history weights would reveal use of future data.", ""]
        rows = [{"strategy": v.name, "passed": "yes" if v.lookahead["passed"] else "NO",
                 "max abs weight diff": v.lookahead["max_abs_diff"]} for v in self.ranked]
        out.append(md_table(pd.DataFrame(rows), {"max abs weight diff": lambda x: f"{x:.1e}"}, index=False))
        out += ["### Walk-forward parameter choices", ""]
        for v in self.focus:
            rows = [{"test window": f"{w['test_start'].date()} → {w['test_end'].date()}",
                     "chosen": ", ".join(f"{k}={w['params'][k]}" for k in v.strategy_cls.param_grid),
                     "train Sharpe": w["train_sharpe"], "test Sharpe": w["test_sharpe"]} for w in v.walk_forward.windows]
            out += [f"*{v.name}*", "", md_table(pd.DataFrame(rows), index=False)]
        out += ["### Evaluation criteria", "", "```json", json.dumps(r.config.criteria.to_dict(), indent=2), "```", "",
                "### Reproduce", "", "```bash",
                f"python -m tradelab research --preset {r.config.name} --source {r.data.source}"
                f" --start {r.config.start}" + (f" --end {r.config.end}" if r.config.end else ""),
                "```", "",
                "Artefacts: `results/ranking.csv`, `results/walk_forward_metrics.csv`, `results/walk_forward_returns.csv`, "
                "`results/sensitivity_*.csv`, `results/summary.json`.", ""]
        return "\n".join(out)


def write_report(res: ResearchResults, out_dir: Optional[str] = None) -> Path:
    return ReportBuilder(res, out_dir).write()
