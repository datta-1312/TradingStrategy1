"""Performance, risk and statistical-significance metrics.

Includes the Probabilistic and Deflated Sharpe Ratios (Bailey & Lopez de Prado,
2012/2014) so that a good-looking Sharpe found after many trials is haircut for
data snooping, and a stationary block bootstrap for Sharpe confidence intervals.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats

EULER_GAMMA = 0.5772156649015329


def _clean(r: pd.Series) -> pd.Series:
    return r.replace([np.inf, -np.inf], np.nan).dropna()


def sharpe_ratio(r: pd.Series, periods: int = 252, rf: float = 0.0) -> float:
    r = _clean(r) - rf / periods
    sd = r.std(ddof=1)
    if len(r) < 2 or not np.isfinite(sd) or sd < 1e-12:
        return 0.0
    return float(r.mean() / sd * math.sqrt(periods))


def sortino_ratio(r: pd.Series, periods: int = 252, rf: float = 0.0) -> float:
    r = _clean(r) - rf / periods
    downside = math.sqrt((np.minimum(r, 0) ** 2).mean()) if len(r) else 0.0
    if downside < 1e-12:
        return 0.0
    return float(r.mean() / downside * math.sqrt(periods))


def cagr(r: pd.Series, periods: int = 252) -> float:
    r = _clean(r)
    if len(r) == 0:
        return 0.0
    growth = float((1 + r).prod())
    years = len(r) / periods
    return growth ** (1 / years) - 1 if growth > 0 and years > 0 else -1.0


def drawdown_series(r: pd.Series) -> pd.Series:
    eq = (1 + _clean(r)).cumprod()
    return eq / eq.cummax() - 1


def max_drawdown(r: pd.Series) -> dict:
    dd = drawdown_series(r)
    if dd.empty:
        return {"max_drawdown": 0.0, "peak": None, "trough": None, "recovery": None, "duration_days": 0}
    trough = dd.idxmin()
    peak = dd.loc[:trough][dd.loc[:trough] == 0].index[-1] if (dd.loc[:trough] == 0).any() else dd.index[0]
    after = dd.loc[trough:]
    recovered = after[after >= 0]
    recovery = recovered.index[0] if len(recovered) else None
    end = recovery if recovery is not None else dd.index[-1]
    # Longest underwater stretch (not necessarily the deepest one).
    underwater = (dd < 0).astype(int)
    longest = int(underwater.groupby((underwater == 0).cumsum()).sum().max())
    return {
        "max_drawdown": float(-dd.min()),
        "peak": peak,
        "trough": trough,
        "recovery": recovery,
        "duration_days": int((end - peak).days) if peak is not None else 0,
        "longest_underwater_bars": longest,
    }


def yearly_returns(r: pd.Series) -> pd.Series:
    r = _clean(r)
    return (1 + r).groupby(r.index.year).prod() - 1


def probabilistic_sharpe(r: pd.Series, sr_benchmark: float = 0.0, periods: int = 252) -> float:
    """P(true Sharpe > sr_benchmark) accounting for sample length, skew and fat tails.

    ``sr_benchmark`` is annualised; the test is done on per-period Sharpe.
    """
    r = _clean(r)
    n = len(r)
    if n < 30 or r.std(ddof=1) < 1e-12:
        return 0.0
    sr = r.mean() / r.std(ddof=1)
    sr_star = sr_benchmark / math.sqrt(periods)
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    z = (sr - sr_star) * math.sqrt(n - 1) / math.sqrt(denom)
    return float(stats.norm.cdf(z))


def expected_max_sharpe(n_trials: int, sr_std_per_period: float) -> float:
    """Expected maximum per-period Sharpe among ``n_trials`` skill-less strategies."""
    if n_trials <= 1 or sr_std_per_period <= 0:
        return 0.0
    z1 = stats.norm.ppf(1 - 1 / n_trials)
    z2 = stats.norm.ppf(1 - 1 / (n_trials * math.e))
    return float(sr_std_per_period * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe(r: pd.Series, n_trials: int, sr_std_per_period: float, periods: int = 252) -> float:
    """PSR against the Sharpe you'd expect from the best of ``n_trials`` random strategies."""
    sr0 = expected_max_sharpe(n_trials, sr_std_per_period) * math.sqrt(periods)
    return probabilistic_sharpe(r, sr_benchmark=sr0, periods=periods)


def bootstrap_sharpe_ci(
    r: pd.Series, periods: int = 252, n_boot: int = 1000, block: int = 20, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """Stationary block bootstrap confidence interval for the annualised Sharpe."""
    x = _clean(r).to_numpy()
    n = len(x)
    if n < 60:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    p = 1.0 / block
    idx = np.empty((n_boot, n), dtype=np.int64)
    idx[:, 0] = rng.integers(0, n, n_boot)
    jumps = rng.random((n_boot, n)) < p
    fresh = rng.integers(0, n, (n_boot, n))
    for t in range(1, n):
        idx[:, t] = np.where(jumps[:, t], fresh[:, t], (idx[:, t - 1] + 1) % n)
    samples = x[idx]
    sd = samples.std(axis=1, ddof=1)
    srs = np.where(sd > 0, samples.mean(axis=1) / np.where(sd > 0, sd, 1) * math.sqrt(periods), 0.0)
    return (float(np.quantile(srs, alpha / 2)), float(np.quantile(srs, 1 - alpha / 2)))


def performance_summary(
    r: pd.Series,
    benchmark: Optional[pd.Series] = None,
    periods: int = 252,
    rf: float = 0.0,
    trades: Optional[pd.DataFrame] = None,
    turnover: Optional[pd.Series] = None,
    exposure: Optional[pd.DataFrame] = None,
    cost: Optional[pd.Series] = None,
) -> dict:
    """Full metric set for one return stream (optionally vs. a benchmark)."""
    r = _clean(r)
    out: dict = {}
    if len(r) < 2:
        return out
    eq = (1 + r).cumprod()
    dd = max_drawdown(r)
    yrs = yearly_returns(r)
    out.update({
        "start": r.index[0].date(),
        "end": r.index[-1].date(),
        "years": len(r) / periods,
        "total_return": float(eq.iloc[-1] - 1),
        "cagr": cagr(r, periods),
        "ann_vol": float(r.std(ddof=1) * math.sqrt(periods)),
        "sharpe": sharpe_ratio(r, periods, rf),
        "sortino": sortino_ratio(r, periods, rf),
        "max_drawdown": dd["max_drawdown"],
        "max_dd_duration_days": dd["duration_days"],
        "calmar": cagr(r, periods) / dd["max_drawdown"] if dd["max_drawdown"] > 0 else 0.0,
        "skew": float(stats.skew(r)),
        "kurtosis": float(stats.kurtosis(r)),
        "var_95": float(-np.quantile(r, 0.05)),
        "cvar_95": float(-r[r <= np.quantile(r, 0.05)].mean()),
        "best_day": float(r.max()),
        "worst_day": float(r.min()),
        "pct_positive_days": float((r > 0).mean()),
        "positive_years": float((yrs > 0).mean()) if len(yrs) else 0.0,
        "worst_year": float(yrs.min()) if len(yrs) else 0.0,
        "psr": probabilistic_sharpe(r, 0.0, periods),
    })
    if trades is not None and len(trades):
        t = trades[(trades["exit"] >= r.index[0]) & (trades["entry"] <= r.index[-1])]
        wins, losses = t.loc[t["pnl"] > 0, "pnl"], t.loc[t["pnl"] <= 0, "pnl"]
        enough = len(t) >= 10  # trade statistics on a handful of spells are meaningless
        out.update({
            "n_trades": int(len(t)),
            "win_rate": float(len(wins) / len(t)) if enough else float("nan"),
            "profit_factor": (float(wins.sum() / -losses.sum()) if losses.sum() < 0 else float("inf"))
            if enough else float("nan"),
            "avg_trade_bars": float(t["bars"].mean()) if len(t) else 0.0,
        })
    else:
        out.update({"n_trades": 0, "win_rate": float("nan"), "profit_factor": float("nan"),
                    "avg_trade_bars": float("nan")})
    if turnover is not None:
        out["annual_turnover"] = float(turnover.loc[r.index[0]: r.index[-1]].mean() * periods)
    if cost is not None:
        out["annual_cost_drag"] = float(cost.loc[r.index[0]: r.index[-1]].mean() * periods)
    if exposure is not None:
        e = exposure.loc[r.index[0]: r.index[-1]]
        out["avg_gross_exposure"] = float(e.abs().sum(axis=1).mean())
        out["avg_net_exposure"] = float(e.sum(axis=1).mean())
        out["time_in_market"] = float((e.abs().sum(axis=1) > 1e-9).mean())
    if benchmark is not None:
        b = _clean(benchmark).reindex(r.index).fillna(0.0)
        var_b = b.var(ddof=1)
        beta = float(r.cov(b) / var_b) if var_b > 0 else 0.0
        active = r - b
        te = active.std(ddof=1) * math.sqrt(periods)
        out.update({
            "benchmark_cagr": cagr(b, periods),
            "benchmark_sharpe": sharpe_ratio(b, periods, rf),
            "benchmark_max_drawdown": max_drawdown(b)["max_drawdown"],
            "beta": beta,
            "alpha_ann": float((r.mean() - rf / periods - beta * (b.mean() - rf / periods)) * periods),
            "correlation": float(r.corr(b)) if r.std() > 0 and b.std() > 0 else 0.0,
            "tracking_error": float(te),
            "information_ratio": float(active.mean() * periods / te) if te > 0 else 0.0,
            "excess_cagr": cagr(r, periods) - cagr(b, periods),
        })
    return out


def rolling_sharpe(r: pd.Series, window: int = 252, periods: int = 252) -> pd.Series:
    m = r.rolling(window).mean()
    s = r.rolling(window).std()
    return (m / s * math.sqrt(periods)).replace([np.inf, -np.inf], np.nan)
