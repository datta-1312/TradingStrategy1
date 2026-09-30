"""Turn validation evidence into an explicit verdict, a score and a ranking.

Verdicts:
  REJECTED  - fails at least one hard criterion (performance, drawdown, costs, bias).
  PROMISING - passes the hard criteria but the statistical evidence is not yet
              conclusive (deflated Sharpe or parameter robustness below the bar).
  VALIDATED - passes every hard and evidence criterion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

from .config import Criteria

STATUS_ORDER = {"VALIDATED": 0, "PROMISING": 1, "REJECTED": 2}


@dataclass
class Check:
    name: str
    value: object
    threshold: str
    passed: Optional[bool]   # None = not applicable
    kind: str                # hard | evidence | soft | info
    as_pct: bool = False     # display the value as a percentage

    @property
    def shown(self) -> str:
        v = self.value
        if isinstance(v, (float, np.floating)):
            if not np.isfinite(v):
                return "n/a"
            return f"{v:.1%}" if self.as_pct else f"{v:.2f}"
        return str(v)


@dataclass
class Verdict:
    status: str
    score: float
    checks: list[Check] = field(default_factory=list)

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.passed is False and c.kind in ("hard", "evidence")]

    @property
    def reasons(self) -> list[str]:
        return [f"{c.name} ({c.shown} vs {c.threshold})" for c in self.failures]


def _clip01(x: float) -> float:
    return float(min(1.0, max(0.0, x))) if np.isfinite(x) else 0.0


def evaluate(v, criteria: Criteria, has_volume: bool) -> Verdict:
    """Score a ``StrategyValidation`` against ``criteria``."""
    wf = v.metrics_wf
    sh = wf.get("sharpe", 0.0)
    b_sh = wf.get("benchmark_sharpe", 0.0)
    mdd = wf.get("max_drawdown", 1.0)
    pos_years = wf.get("positive_years", 0.0)
    resil = v.stress.get("cost_resilience", 0.0)
    dsr = v.significance.get("dsr", 0.0)
    robust = v.robustness.get("median_to_best", 0.0)
    max_part = float(v.walk_forward.result.participation.loc[v.walk_forward.start:].max())
    delay = v.stress.get("delay_1_session", np.nan)
    trend_reg = v.regimes[v.regimes["dimension"] == "trend"] if len(v.regimes) else pd.DataFrame()
    reg_frac = float((trend_reg["sharpe"] > 0).mean()) if len(trend_reg) else float("nan")
    top_share = v.concentration.get("top_asset_share", float("nan"))
    n_assets = len(v.walk_forward.targets.columns)
    is_sh = v.metrics_is.get("sharpe", 0.0)

    c = criteria
    checks = [
        Check("Walk-forward Sharpe", sh, f">= {c.min_sharpe}", sh >= c.min_sharpe, "hard"),
        Check("Sharpe minus benchmark Sharpe", sh - b_sh, "> 0", sh > b_sh, "hard"),
        Check("Max drawdown", mdd, f"< {c.max_drawdown:.0%}", mdd < c.max_drawdown, "hard", True),
        Check("Positive calendar years", pos_years, f">= {c.min_positive_years:.0%}",
              pos_years >= c.min_positive_years, "hard", True),
        Check("Cost resilience (Sharpe at 2x costs / base)", resil, f">= {c.min_cost_resilience}",
              resil >= c.min_cost_resilience, "hard"),
        Check("No look-ahead bias (truncation test)", v.lookahead["max_abs_diff"], "== 0", v.lookahead["passed"],
              "hard"),
        Check("Liquidity (max share of ADV traded)", max_part, f"<= {c.max_adv_participation:.0%}",
              (max_part <= c.max_adv_participation) if has_volume else None, "hard", True),
        Check("Deflated Sharpe (multiple-testing adjusted)", dsr, f">= {c.min_deflated_sharpe}",
              dsr >= c.min_deflated_sharpe, "evidence"),
        Check("Parameter robustness (median/best grid Sharpe)", robust, f">= {c.min_param_robustness}",
              robust >= c.min_param_robustness, "evidence"),
        Check("Preferred Sharpe", sh, f">= {c.preferred_sharpe}", sh >= c.preferred_sharpe, "soft"),
        Check("CAGR minus benchmark CAGR", wf.get("excess_cagr", 0.0), "> 0", wf.get("excess_cagr", 0.0) > 0, "soft", True),
        Check("Positive Sharpe across trend regimes", reg_frac, ">= 67% of regimes",
              (reg_frac >= 0.67) if np.isfinite(reg_frac) else None, "soft", True),
        Check("Single-asset P&L share", top_share, f"<= {c.max_asset_pnl_share:.0%}",
              (top_share <= c.max_asset_pnl_share) if n_assets > 1 and np.isfinite(top_share) else None, "soft",
              True),
        Check("Survives 1-session execution delay", delay, ">= 50% of base Sharpe",
              (delay >= 0.5 * sh) if np.isfinite(delay) and sh > 0 else (False if sh > 0 else None), "soft"),
        Check("In-sample -> walk-forward decay", (sh / is_sh) if is_sh > 0 else float("nan"), ">= 0.5",
              (sh / is_sh >= 0.5) if is_sh > 0 else None, "soft"),
        Check("Economic rationale documented", "yes" if v.strategy_cls.thesis else "no", "required",
              bool(v.strategy_cls.thesis), "info"),
    ]

    hard_fail = any(ch.passed is False for ch in checks if ch.kind == "hard")
    evidence_fail = any(ch.passed is False for ch in checks if ch.kind == "evidence")
    status = "REJECTED" if hard_fail else ("PROMISING" if evidence_fail else "VALIDATED")

    score = (
        30 * _clip01(sh / 1.5)
        + 15 * _clip01(dsr)
        + 10 * _clip01(robust)
        + 10 * _clip01(1 - mdd / 0.5)
        + 10 * _clip01(resil)
        + 10 * _clip01(sh - b_sh + 0.5)
        + 5 * _clip01(pos_years)
        + 5 * (_clip01(reg_frac) if np.isfinite(reg_frac) else 0.5)
        + 5 * (_clip01(1 - top_share) if n_assets > 1 and np.isfinite(top_share) else 0.5)
    )
    return Verdict(status, round(float(score), 1), checks)


def rank_strategies(validations: list) -> pd.DataFrame:
    rows = []
    for v in validations:
        wf = v.metrics_wf
        rows.append({
            "strategy": v.name,
            "status": v.verdict.status,
            "score": v.verdict.score,
            "wf_sharpe": wf.get("sharpe"),
            "wf_cagr": wf.get("cagr"),
            "wf_max_dd": wf.get("max_drawdown"),
            "benchmark_sharpe": wf.get("benchmark_sharpe"),
            "dsr": v.significance.get("dsr"),
            "psr": v.significance.get("psr"),
            "robustness": v.robustness.get("median_to_best"),
            "cost_x2_sharpe": v.stress.get("costs_x2"),
            "annual_turnover": wf.get("annual_turnover"),
            "main_issue": v.verdict.reasons[0] if v.verdict.reasons else "",
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["_order"] = df["status"].map(STATUS_ORDER)
    df = df.sort_values(["_order", "score"], ascending=[True, False]).drop(columns="_order")
    df.insert(0, "rank", range(1, len(df) + 1))
    return df.reset_index(drop=True)


def select_final(validations: list, ranking: pd.DataFrame, top_n: int, max_corr: float = 0.8) -> list:
    """Top non-rejected strategies, one variant per base strategy, skipping near-duplicates
    (walk-forward return correlation > max_corr) so the final list is genuinely diversified."""
    by_name = {v.name: v for v in validations}
    chosen = []
    for name in ranking["strategy"]:
        v = by_name[name]
        if v.verdict.status == "REJECTED":
            continue
        base = name.split("+")[0]
        same = next((c for c in chosen if c.name.split("+")[0] == base), None)
        if same is not None:
            v.iteration_note += f" Not selected: a higher-ranked variant of {base} ({same.name}) was chosen."
            continue
        dup = False
        for c in chosen:
            both = pd.concat([v.walk_forward.returns, c.walk_forward.returns], axis=1).dropna()
            if len(both) > 60 and both.corr().iloc[0, 1] > max_corr:
                dup = True
                v.iteration_note += f" Not selected: >{max_corr:.0%} correlated with {c.name}."
                break
        if not dup:
            chosen.append(v)
        if len(chosen) == top_n:
            break
    return chosen
