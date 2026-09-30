"""Directional time-series strategies: trend following, breakouts, volatility timing."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import MarketData
from .base import Strategy, annualised_vol, cap_gross, hold_between_rebalances, state_machine


def _n_available(close: pd.DataFrame) -> pd.Series:
    return close.notna().sum(axis=1).clip(lower=1)


class TimeSeriesMomentum(Strategy):
    name = "trend_tsmom"
    family = "Trend following (time-series momentum)"
    thesis = (
        "Prices trend because information diffuses slowly, investors under-react and then herd, and "
        "risk-transfer flows (hedging, deleveraging, rebalancing) persist for weeks to months. Time-series "
        "momentum is documented across asset classes over a century of data and tends to profit in "
        "prolonged bear markets, which makes it a diversifier rather than a pure return engine."
    )
    rules = (
        "Long an asset when its trailing `lookback`-day total return is positive; flat otherwise "
        "(short instead if `allow_short`)."
    )
    sizing = (
        "Inverse volatility: each asset targets `vol_target`/N annualised vol from a 63-day estimate; "
        "gross exposure capped at 100% (no leverage)."
    )
    rebalance = "Every `rebalance` trading days."
    invalidation = (
        "Persistent whipsaw losses in range-bound markets with sharp V-shaped reversals; several years of "
        "negative out-of-sample returns during markets that did trend would falsify the premise."
    )
    references = [
        "Moskowitz, Ooi & Pedersen (2012), 'Time Series Momentum', JFE",
        "Hurst, Ooi & Pedersen (2017), 'A Century of Evidence on Trend-Following Investing'",
    ]
    default_params = {"lookback": 252, "vol_target": 0.15, "rebalance": 5, "vol_window": 63,
                      "allow_short": False, "max_gross": 1.0}
    param_grid = {"lookback": [63, 126, 252], "vol_target": [0.10, 0.15], "rebalance": [5, 21]}

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        close = data.close
        mom = close / close.shift(p["lookback"]) - 1
        signal = (mom > 0).astype(float)
        if p["allow_short"]:
            signal -= (mom < 0).astype(float)
        vol = annualised_vol(close.pct_change(fill_method=None), p["vol_window"])
        w = signal * (p["vol_target"] / vol).div(_n_available(close), axis=0)
        w = w.where(mom.notna() & vol.notna(), 0.0).clip(-1, 1)
        return hold_between_rebalances(cap_gross(w, p["max_gross"]), p["rebalance"])


class DonchianBreakout(Strategy):
    name = "donchian_breakout"
    family = "Breakout (price-channel trend)"
    thesis = (
        "A close beyond the recent trading range signals new information or a shift in supply/demand that "
        "the market has not yet fully priced; asymmetric exits (a shorter exit channel) cut losers quickly "
        "and let winners run, producing positively skewed payoffs. This is the classic Turtle rule set."
    )
    rules = (
        "Enter long when the close exceeds the highest high of the prior `entry` sessions; exit when the "
        "close falls below the lowest low of the prior `exit` sessions."
    )
    sizing = (
        "Position size fixed at entry: `vol_target`/N divided by the asset's 63-day volatility, capped at "
        "100% per asset and 100% gross."
    )
    rebalance = "Event driven: trades only on breakout entries and channel exits."
    invalidation = (
        "Breakouts that systematically fail (false breakouts > ~65% with no fat right tail) or a win/loss "
        "size ratio that collapses below ~1.5 out of sample."
    )
    references = [
        "Donchian (1960); Faith (2007), 'Way of the Turtle'",
        "Brock, Lakonishok & LeBaron (1992), 'Simple Technical Trading Rules', JF",
    ]
    default_params = {"entry": 55, "exit": 20, "vol_target": 0.15, "vol_window": 63, "max_gross": 1.0}
    param_grid = {"entry": [20, 55, 100], "exit": [10, 20, 50], "vol_target": [0.10, 0.20]}

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        return params["exit"] < params["entry"]

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        close = data.close
        upper = data.high.rolling(p["entry"], min_periods=p["entry"]).max().shift(1)
        lower = data.low.rolling(p["exit"], min_periods=p["exit"]).min().shift(1)
        pos = state_machine(close > upper, close < lower)
        vol = annualised_vol(close.pct_change(fill_method=None), p["vol_window"])
        size = (p["vol_target"] / vol).div(_n_available(close), axis=0).clip(upper=1.0)
        entry_day = pos.diff().fillna(pos) > 0
        size_at_entry = size.where(entry_day).ffill()
        w = (pos * size_at_entry).fillna(0.0)
        return cap_gross(w, p["max_gross"])


class VolatilityManaged(Strategy):
    name = "vol_managed"
    family = "Volatility timing (risk management as a return source)"
    thesis = (
        "Volatility clusters and is forecastable, while expected returns do not rise proportionally when "
        "volatility spikes. Scaling exposure inversely to recent variance therefore improves risk-adjusted "
        "returns and shrinks crash exposure. The edge comes from risk timing, not from direction."
    )
    rules = "Always long; exposure = min(max_leverage, vol_target / realised vol) per asset, in 10% steps."
    sizing = "Equal risk budget per asset (1/N of the scaled exposure); leverage only if max_leverage > 1."
    rebalance = "Weekly (every 5 sessions); exposure quantised to 10% steps to limit churn."
    invalidation = (
        "Out-of-sample Sharpe no better than buy-and-hold, or losses concentrated in fast 'vol-of-vol' "
        "spikes where exposure is cut only after the damage (e.g. single-day crashes)."
    )
    references = [
        "Moreira & Muir (2017), 'Volatility-Managed Portfolios', JF",
        "Harvey et al. (2018), 'The Impact of Volatility Targeting', JPM",
    ]
    default_params = {"vol_target": 0.12, "vol_window": 21, "max_leverage": 1.0, "rebalance": 5}
    param_grid = {"vol_window": [21, 63], "vol_target": [0.10, 0.15, 0.20], "max_leverage": [1.0, 1.5]}

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        close = data.close
        vol = annualised_vol(close.pct_change(fill_method=None), p["vol_window"])
        scale = (p["vol_target"] / vol).clip(upper=p["max_leverage"])
        scale = np.floor(scale * 10) / 10
        w = scale.div(_n_available(close), axis=0).where(vol.notna(), 0.0)
        return hold_between_rebalances(w, p["rebalance"])
