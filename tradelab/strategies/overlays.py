"""Risk overlays used in the iteration step to try to repair weak strategies.

An overlay wraps a base strategy class and adds its own parameters to the grid, so
every overlay variant is counted as additional trials in the deflated Sharpe ratio.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from ..data import MarketData
from .base import Strategy, state_machine

OVERLAYS = {
    "regime": (
        "Market regime filter: only hold positions while the benchmark trades above its `regime_sma`-day "
        "moving average (2% hysteresis band). Addresses drawdowns concentrated in bear markets."
    ),
    "voltarget": (
        "Portfolio volatility targeting: scale the whole book to `port_vol` annualised volatility using "
        "the trailing 63-day volatility of the base book (max 1.0x, 10% steps). Addresses deep drawdowns "
        "and volatility clustering."
    ),
}


def make_overlay(base_cls: type[Strategy], kind: str) -> type[Strategy]:
    if kind not in OVERLAYS:
        raise KeyError(f"Unknown overlay {kind!r}")
    base_keys = list(base_cls.param_grid)[:2]  # keep the grid small: 2 base dims x overlay dim
    extra = {"regime_sma": [150, 200]} if kind == "regime" else {"port_vol": [0.08, 0.12]}

    class Overlay(Strategy):
        name = f"{base_cls.name}+{kind}"
        family = f"{base_cls.family} + {kind} overlay"
        thesis = f"{base_cls.thesis} OVERLAY: {OVERLAYS[kind]}"
        rules = f"{base_cls.rules} {OVERLAYS[kind]}"
        sizing = base_cls.sizing
        rebalance = base_cls.rebalance
        invalidation = base_cls.invalidation
        references = base_cls.references
        min_assets = base_cls.min_assets
        long_only = base_cls.long_only
        default_params = {**base_cls.default_params, **{k: v[-1] for k, v in extra.items()}}
        param_grid = {**{k: base_cls.param_grid[k] for k in base_keys}, **extra}
        base = base_cls

        @classmethod
        def valid_params(cls, params: dict) -> bool:
            return base_cls.valid_params({k: params[k] for k in base_cls.default_params})

        def generate_weights(self, data: MarketData) -> pd.DataFrame:
            base_params = {k: self.params[k] for k in base_cls.default_params}
            w = base_cls(**base_params).generate_weights(data)
            if kind == "regime":
                bench = data.benchmark
                sma = bench.rolling(self.params["regime_sma"], min_periods=self.params["regime_sma"]).mean()
                on = state_machine((bench > sma).to_frame(), (bench < 0.98 * sma).to_frame()).iloc[:, 0]
                return w.mul(on, axis=0)
            rets = data.close.pct_change(fill_method=None).fillna(0.0)
            book = (w.shift(1).fillna(0.0) * rets).sum(axis=1)
            vol = book.rolling(63, min_periods=21).std() * math.sqrt(252)
            scale = (self.params["port_vol"] / vol).clip(upper=1.0)
            scale = (np.floor(scale * 10) / 10).fillna(1.0)
            return w.mul(scale, axis=0)

    Overlay.__name__ = f"{base_cls.__name__}_{kind}"
    return Overlay
