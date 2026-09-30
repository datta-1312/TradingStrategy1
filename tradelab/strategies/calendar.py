"""Calendar / event-driven effects."""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..data import MarketData
from .base import Strategy


def turn_of_month_mask(index: pd.DatetimeIndex, days_before: int, days_after: int) -> np.ndarray:
    """True if the *next* business day lies in the turn-of-month window.

    Uses only the business-day calendar (known in advance), never prices or the
    future shape of the data index, so it is strictly causal.
    """
    nxt = (index + pd.offsets.BDay(1)).values.astype("datetime64[D]")
    month_start = nxt.astype("datetime64[M]").astype("datetime64[D]")
    next_month = (nxt.astype("datetime64[M]") + 1).astype("datetime64[D]")
    day_of_month = np.busday_count(month_start, nxt) + 1      # 1 = first business day
    days_to_end = np.busday_count(nxt, next_month)              # 1 = last business day
    return (days_to_end <= days_before) | (day_of_month <= days_after)


class TurnOfMonth(Strategy):
    name = "turn_of_month"
    family = "Calendar / flow-driven event effect"
    thesis = (
        "Equity returns cluster around the turn of the month: salaries, pension contributions and fund "
        "inflows are invested on predictable dates, and institutions window-dress before month end. The "
        "strategy is invested only a fraction of the time, so it earns a large share of the market's return "
        "with far less exposure."
    )
    rules = "Long during the last `days_before` and first `days_after` business days of each month; cash otherwise."
    sizing = "Equal weight across available assets; fully invested inside the window, flat outside."
    rebalance = "Calendar driven: two trades per month."
    invalidation = (
        "Well-known calendar anomalies can be arbitraged away after publication; if the in-window average "
        "daily return is no longer higher than out-of-window returns in the walk-forward period, drop it."
    )
    references = [
        "Ariel (1987), 'A Monthly Effect in Stock Returns', JFE",
        "Lakonishok & Smidt (1988), 'Are Seasonal Anomalies Real?', RFS",
        "McConnell & Xu (2008), 'Equity Returns at the Turn of the Month', FAJ",
    ]
    default_params = {"days_before": 2, "days_after": 3}
    param_grid = {"days_before": [1, 2, 3], "days_after": [1, 3, 5]}

    def generate_weights(self, data: MarketData) -> pd.DataFrame:
        p = self.params
        close = data.close
        mask = turn_of_month_mask(close.index, p["days_before"], p["days_after"])
        avail = close.notna().astype(float)
        w = avail.div(avail.sum(axis=1).clip(lower=1), axis=0)
        return w.mul(mask.astype(float), axis=0)
