"""Single-asset short-term mean reversion."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import MarketData
from .base import Strategy, rsi


def hold_with_max_bars(entries: np.ndarray, exits: np.ndarray, max_hold: int) -> np.ndarray:
    """In-position flag: open on entry, close on exit or after ``max_hold`` bars."""
    T, N = entries.shape
    out = np.zeros((T, N))
    for j in range(N):
        held = 0
        for t in range(T):
            if held:
                held += 1
                if exits[t, j] or held > max_hold:
                    held = 0
            elif entries[t, j]:
                held = 1
            out[t, j] = 1.0 if held else 0.0
    return out


class RSIPullback(Strategy):
    name = "rsi_pullback"
    family = "Short-term mean reversion (buy the dip in an uptrend)"
    thesis = (
        "Short-horizon selling pressure (stop-outs, forced liquidations, over-reaction to noise) pushes "
        "prices below fair value for a few days; within an established uptrend these dips tend to be bought. "
        "The trend filter avoids catching falling knives in bear markets."
    )
    rules = (
        "Enter long when RSI(`rsi_len`) < `entry` and close > `trend_len`-day SMA; exit when RSI > `exit` "
        "or after `max_hold` sessions."
    )
    sizing = "1/N of capital per asset slot; no leverage."
    rebalance = "Event driven (signal checked every close)."
    invalidation = (
        "If the edge disappears with a one-day execution delay or at 2x costs, it is too thin to trade; "
        "a regime of persistent momentum crashes (dips keep falling) invalidates the premise."
    )
    references = [
        "Connors & Alvarez (2009), 'Short Term Trading Strategies That Work'",
        "Jegadeesh (1990), 'Evidence of Predictable Behavior of Security Returns', JF",
    ]
    default_params = {"rsi_len": 2, "entry": 10, "exit": 70, "trend_len": 200, "max_hold": 10}
    param_grid = {"rsi_len": [2, 3], "entry": [5, 10, 20], "exit": [50, 70]}

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        close = data.close
        r = rsi(close, p["rsi_len"])
        trend = close > close.rolling(p["trend_len"], min_periods=p["trend_len"]).mean()
        entries = ((r < p["entry"]) & trend).to_numpy()
        exits = (r > p["exit"]).to_numpy()
        pos = hold_with_max_bars(entries, exits, p["max_hold"])
        n = close.notna().sum(axis=1).clip(lower=1).to_numpy()[:, None]
        return pd.DataFrame(pos / n, index=close.index, columns=close.columns)
