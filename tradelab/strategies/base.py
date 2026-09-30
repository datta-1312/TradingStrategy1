"""Strategy interface and shared signal helpers.

A strategy turns market data into a panel of *target weights* (dates x assets),
decided with information available at each date's close. Holding periods are
expressed by repeating the previous target; the backtester trades only when the
target changes. Every strategy documents its economic thesis, rules and
invalidation conditions so the report can state *why* an edge might exist.
"""
from __future__ import annotations

import itertools
import math
import random
from typing import ClassVar

import numpy as np
import pandas as pd

from ..data import MarketData


class Strategy:
    name: ClassVar[str] = "base"
    family: ClassVar[str] = ""
    thesis: ClassVar[str] = ""          # why the edge might exist
    rules: ClassVar[str] = ""           # entry / exit logic in plain words
    sizing: ClassVar[str] = ""
    rebalance: ClassVar[str] = ""
    invalidation: ClassVar[str] = ""    # evidence that would kill the thesis
    references: ClassVar[list[str]] = []
    min_assets: ClassVar[int] = 1
    long_only: ClassVar[bool] = True
    default_params: ClassVar[dict] = {}
    param_grid: ClassVar[dict[str, list]] = {}

    def __init__(self, **params):
        unknown = set(params) - set(self.default_params)
        if unknown:
            raise ValueError(f"{self.name}: unknown parameters {sorted(unknown)}")
        self.params = {**self.default_params, **params}

    # ----------------------------------------------------------------- interface
    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        raise NotImplementedError

    @classmethod
    def applicable(cls, data: MarketData) -> tuple[bool, str]:
        n = len(data.assets)
        if n < cls.min_assets:
            return False, f"needs >= {cls.min_assets} assets (universe has {n})"
        return True, ""

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        """Override to prune nonsensical grid combinations."""
        return True

    @classmethod
    def grid(cls, max_combos: int = 24, seed: int = 0) -> list[dict]:
        """Parameter grid (deterministically subsampled if larger than ``max_combos``)."""
        keys = list(cls.param_grid)
        combos = [dict(zip(keys, vals)) for vals in itertools.product(*(cls.param_grid[k] for k in keys))]
        combos = [{**cls.default_params, **c} for c in combos]
        combos = [c for c in combos if cls.valid_params(c)]
        if cls.default_params not in combos and cls.valid_params(cls.default_params):
            combos.insert(0, dict(cls.default_params))
        if len(combos) > max_combos:
            rest = [c for c in combos if c != cls.default_params]
            random.Random(seed).shuffle(rest)
            combos = [dict(cls.default_params)] + rest[: max_combos - 1]
        return combos

    @property
    def label(self) -> str:
        diff = {k: v for k, v in self.params.items() if k in self.param_grid}
        inner = ",".join(f"{k}={v}" for k, v in diff.items())
        return f"{self.name}({inner})"

    def describe(self) -> dict:
        return {
            "name": self.name, "family": self.family, "thesis": self.thesis, "rules": self.rules,
            "sizing": self.sizing, "rebalance": self.rebalance, "invalidation": self.invalidation,
            "references": self.references, "params": self.params, "long_only": self.long_only,
        }


# --------------------------------------------------------------------- helpers


def annualised_vol(returns: pd.DataFrame, window: int, periods: int = 252) -> pd.DataFrame:
    return returns.rolling(window, min_periods=max(5, window // 2)).std() * math.sqrt(periods)


def hold_between_rebalances(weights: pd.DataFrame, every: int, offset: int = 0) -> pd.DataFrame:
    """Keep only every ``every``-th row as a decision, carrying it forward in between."""
    if every <= 1:
        return weights
    skip = np.ones(len(weights), dtype=bool)
    skip[offset::every] = False
    held = weights.fillna(0.0)
    held.iloc[skip] = np.nan
    return held.ffill().fillna(0.0)


def cap_gross(weights: pd.DataFrame, max_gross: float) -> pd.DataFrame:
    gross = weights.abs().sum(axis=1)
    scale = (max_gross / gross).where(gross > max_gross, 1.0)
    return weights.mul(scale, axis=0)


def state_machine(entries: pd.DataFrame, exits: pd.DataFrame) -> pd.DataFrame:
    """1 after an entry signal until the next exit signal (exit wins ties)."""
    state = pd.DataFrame(np.nan, index=entries.index, columns=entries.columns)
    state = state.mask(entries.fillna(False).astype(bool), 1.0)
    state = state.mask(exits.fillna(False).astype(bool), 0.0)
    return state.ffill().fillna(0.0)


def rsi(close: pd.DataFrame, length: int) -> pd.DataFrame:
    """Wilder's RSI."""
    delta = close.diff()
    up = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    down = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rs = up / down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(down > 0, 100.0).where(up.notna())


def rank_select(score: pd.DataFrame, fraction: float, top: bool = True, min_count: int = 1) -> pd.DataFrame:
    """Boolean mask of the top (or bottom) ``fraction`` of valid assets per row."""
    valid = score.notna()
    n_valid = valid.sum(axis=1)
    k = np.maximum(min_count, np.ceil(n_valid * fraction)).where(n_valid > 0, 0)
    ranks = score.rank(axis=1, ascending=not top, method="first")
    return ranks.le(k, axis=0) & valid
