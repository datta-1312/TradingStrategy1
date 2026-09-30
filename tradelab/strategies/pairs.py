"""Market-neutral statistical arbitrage on cointegrated pairs."""
from __future__ import annotations

import itertools
import zlib

import numpy as np
import pandas as pd

from ..data import MarketData
from .base import Strategy


def adf_tstat(series: np.ndarray) -> float:
    """Dickey-Fuller t-statistic (with constant, no lags) for a unit root in ``series``."""
    y = np.diff(series)
    x = series[:-1]
    X = np.column_stack([np.ones_like(x), x])
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ beta
    dof = len(y) - 2
    if dof <= 0:
        return 0.0
    s2 = resid @ resid / dof
    cov = s2 * np.linalg.pinv(X.T @ X)
    se = np.sqrt(cov[1, 1])
    return float(beta[1] / se) if se > 0 else 0.0


def select_pairs(logp: pd.DataFrame, n_pairs: int, max_adf_t: float) -> list[tuple[str, str, float, float]]:
    """Engle-Granger style screen: rank pairs by ADF t-stat of the OLS hedge residual."""
    cols = [c for c in logp.columns if logp[c].notna().all()]
    found = []
    for a, b in itertools.combinations(cols, 2):
        y, x = logp[a].to_numpy(), logp[b].to_numpy()
        vx = np.var(x)
        if vx <= 0:
            continue
        beta = np.cov(x, y, bias=True)[0, 1] / vx
        if beta <= 0:
            continue
        t = adf_tstat(y - beta * x)
        if t < max_adf_t:
            found.append((a, b, beta, t))
    found.sort(key=lambda r: r[3])
    chosen, used = [], set()
    for a, b, beta, t in found:
        if a in used or b in used:
            continue
        chosen.append((a, b, beta, t))
        used |= {a, b}
        if len(chosen) == n_pairs:
            break
    return chosen


_FORMATION_CACHE: dict[tuple, list] = {}


def formation_schedule(logp: pd.DataFrame, formation: int, reform: int, n_pairs: int,
                       max_adf_t: float) -> list[tuple[int, list]]:
    """Pair selections for every formation date (cached: independent of the trading parameters)."""
    key = (zlib.crc32(np.ascontiguousarray(logp.to_numpy()).tobytes()), logp.shape, formation, reform,
           n_pairs, max_adf_t)
    if key not in _FORMATION_CACHE:
        if len(_FORMATION_CACHE) > 16:
            _FORMATION_CACHE.clear()
        _FORMATION_CACHE[key] = [
            (f_end, select_pairs(logp.iloc[f_end - formation: f_end], n_pairs, max_adf_t))
            for f_end in range(formation, len(logp), reform)
        ]
    return _FORMATION_CACHE[key]


class PairsTrading(Strategy):
    name = "pairs_stat_arb"
    family = "Statistical arbitrage (pairs / market neutral)"
    thesis = (
        "Economically linked assets (same industry, same risk drivers) share a long-run equilibrium; "
        "temporary divergences caused by idiosyncratic flows tend to close. Trading the spread earns a "
        "liquidity-provision premium with little market beta, diversifying directional strategies."
    )
    rules = (
        "Every `reform` sessions, pick up to `n_pairs` disjoint pairs whose log-price hedge residual over the "
        "past `formation` sessions has an ADF t-stat below `max_adf_t`. Trade the spread when its "
        "`z_window`-day z-score exceeds +/-`entry_z`; exit when it reverts inside `exit_z`. Open positions "
        "are closed at the end of each trading period."
    )
    sizing = "Each pair gets 1/`n_pairs` of gross capital, split long/short by the hedge ratio (dollar neutral-ish)."
    rebalance = "Pair formation every `reform` sessions; signals checked every close."
    invalidation = (
        "Structural breaks (mergers, regulation, business-model change) that permanently decouple a pair; "
        "if out-of-sample spreads stop reverting (losing trades hit the period end rather than exit_z)."
    )
    references = [
        "Gatev, Goetzmann & Rouwenhorst (2006), 'Pairs Trading', RFS",
        "Engle & Granger (1987), 'Co-integration and Error Correction', Econometrica",
    ]
    min_assets = 2
    long_only = False
    default_params = {"formation": 252, "reform": 63, "n_pairs": 3, "z_window": 20, "entry_z": 2.0,
                      "exit_z": 0.5, "max_adf_t": -3.0}
    param_grid = {"z_window": [20, 60], "entry_z": [1.5, 2.0, 2.5], "exit_z": [0.0, 0.5]}

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        logp = np.log(data.close)
        T, N = logp.shape
        cols = list(logp.columns)
        W = np.zeros((T, N))
        n_pairs = max(1, min(p["n_pairs"], N // 2))
        for f_end, pairs in formation_schedule(logp, p["formation"], p["reform"], n_pairs, p["max_adf_t"]):
            period_end = f_end + p["reform"]  # fixed by the calendar, not by how much data exists
            stop = min(period_end, T)
            for a, b, beta, _ in pairs:
                lo = max(0, f_end - p["z_window"])
                spread = (logp[a] - beta * logp[b]).iloc[lo:stop]
                mean = spread.rolling(p["z_window"], min_periods=p["z_window"]).mean()
                std = spread.rolling(p["z_window"], min_periods=p["z_window"]).std()
                z = ((spread - mean) / std).iloc[f_end - lo:].to_numpy()
                ia, ib = cols.index(a), cols.index(b)
                wa, wb = 1 / (1 + beta) / n_pairs, -beta / (1 + beta) / n_pairs
                pos = 0
                for k, zk in enumerate(z):
                    t = f_end + k
                    last_bar = t == period_end - 1
                    if np.isfinite(zk):
                        if pos == 0 and not last_bar:
                            pos = -1 if zk > p["entry_z"] else (1 if zk < -p["entry_z"] else 0)
                        elif pos == 1 and zk >= -p["exit_z"]:
                            pos = 0
                        elif pos == -1 and zk <= p["exit_z"]:
                            pos = 0
                    if last_bar:
                        pos = 0
                    W[t, ia] += pos * wa
                    W[t, ib] += pos * wb
        return pd.DataFrame(W, index=logp.index, columns=cols)
