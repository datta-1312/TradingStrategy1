"""Relative-value strategies that rank assets against each other."""
from __future__ import annotations

import pandas as pd

from ..data import MarketData
from .base import Strategy, annualised_vol, hold_between_rebalances, rank_select


class CrossSectionalMomentum(Strategy):
    name = "xs_momentum"
    family = "Relative strength rotation (cross-sectional momentum)"
    thesis = (
        "Assets that outperformed peers over the past 3-12 months tend to keep outperforming for a few "
        "months: slow diffusion of sector/industry news, analyst under-reaction and institutional flows "
        "chasing performance. Skipping the most recent month avoids the short-term reversal effect. With "
        "`abs_filter` it becomes 'dual momentum': only hold winners whose own trend is positive."
    )
    rules = (
        "Rank assets by return from t-`lookback` to t-`skip`; hold the top `top_frac`. With `abs_filter`, "
        "a selected asset with a negative score is replaced by cash."
    )
    sizing = "Equal weight across the selected slots; gross exposure <= 100%."
    rebalance = "Every `rebalance` trading days (monthly by default)."
    invalidation = (
        "Momentum crashes: violent rebounds of past losers after market bottoms (2009, 2020) - if losses in "
        "rebounds dominate and the out-of-sample spread between winners and losers turns negative."
    )
    references = [
        "Jegadeesh & Titman (1993), 'Returns to Buying Winners and Selling Losers', JF",
        "Moskowitz & Grinblatt (1999), 'Do Industries Explain Momentum?', JF",
        "Antonacci (2014), 'Dual Momentum Investing'",
    ]
    min_assets = 4
    default_params = {"lookback": 252, "skip": 21, "top_frac": 0.33, "abs_filter": False, "rebalance": 21}
    param_grid = {"lookback": [63, 126, 252], "skip": [0, 21], "top_frac": [0.25, 0.5],
                  "abs_filter": [False, True]}

    @classmethod
    def valid_params(cls, params: dict) -> bool:
        return params["skip"] < params["lookback"]

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        close = data.close
        score = close.shift(p["skip"]) / close.shift(p["lookback"]) - 1
        selected = rank_select(score, p["top_frac"], top=True)
        slots = selected.sum(axis=1).clip(lower=1)
        if p["abs_filter"]:
            selected &= score > 0
        w = selected.astype(float).div(slots, axis=0)
        return hold_between_rebalances(w, p["rebalance"])


class CrossSectionalReversal(Strategy):
    name = "xs_reversal"
    family = "Short-term reversal (liquidity provision)"
    thesis = (
        "Over days to a few weeks, the biggest relative losers tend to bounce: prices overshoot when "
        "liquidity-demanding traders push them, and the reversal is the compensation earned by liquidity "
        "providers. The edge is well documented but small per trade and cost-sensitive."
    )
    rules = "Every `rebalance` sessions, buy the bottom `bottom_frac` of assets by `lookback`-day return."
    sizing = "Equal weight across the selected losers; gross exposure <= 100%."
    rebalance = "Every `rebalance` trading days."
    invalidation = (
        "Edge vanishing after realistic costs (net Sharpe < 0.3 at 2x costs), or losers continuing to lose "
        "(reversal turning into momentum) out of sample."
    )
    references = [
        "Lehmann (1990), 'Fads, Martingales, and Market Efficiency', QJE",
        "Nagel (2012), 'Evaporating Liquidity', RFS",
    ]
    min_assets = 5
    default_params = {"lookback": 5, "bottom_frac": 0.25, "rebalance": 5}
    param_grid = {"lookback": [5, 10, 21], "bottom_frac": [0.2, 0.33], "rebalance": [5, 10]}

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        close = data.close
        score = close / close.shift(p["lookback"]) - 1
        selected = rank_select(score, p["bottom_frac"], top=False)
        w = selected.astype(float).div(selected.sum(axis=1).clip(lower=1), axis=0)
        return hold_between_rebalances(w, p["rebalance"])


class LowVolatility(Strategy):
    name = "low_vol"
    family = "Defensive / low-volatility anomaly"
    thesis = (
        "Low-risk assets have historically delivered similar or higher returns than high-risk assets, "
        "contradicting CAPM. Explanations: leverage-constrained investors bid up high-beta assets, "
        "benchmark-hugging managers avoid low-beta ones, and investors overpay for lottery-like upside."
    )
    rules = "Hold the `frac` of assets with the lowest `vol_window`-day realised volatility."
    sizing = "Equal weight or inverse-volatility weight among the selected assets; fully invested."
    rebalance = "Every `rebalance` trading days (monthly by default)."
    invalidation = (
        "Underperformance driven by rate shocks (low-vol assets are bond proxies) or crowding: if the "
        "strategy's drawdowns stop being smaller than the benchmark's out of sample, the defensive premise fails."
    )
    references = [
        "Frazzini & Pedersen (2014), 'Betting Against Beta', JFE",
        "Baker, Bradley & Wurgler (2011), 'Benchmarks as Limits to Arbitrage', FAJ",
    ]
    min_assets = 3
    default_params = {"vol_window": 126, "frac": 0.33, "weighting": "inverse_vol", "rebalance": 21}
    param_grid = {"vol_window": [63, 126, 252], "frac": [0.33, 0.5], "weighting": ["equal", "inverse_vol"]}

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        vol = annualised_vol(data.close.pct_change(fill_method=None), p["vol_window"])
        selected = rank_select(vol, p["frac"], top=False)
        raw = (1 / vol).where(selected, 0.0) if p["weighting"] == "inverse_vol" else selected.astype(float)
        w = raw.div(raw.sum(axis=1).where(lambda s: s > 0, 1.0), axis=0).fillna(0.0)
        return hold_between_rebalances(w, p["rebalance"])
