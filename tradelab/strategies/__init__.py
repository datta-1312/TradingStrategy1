"""Strategy library: fundamentally different sources of edge, one class each."""
from __future__ import annotations

from .base import Strategy
from .calendar import TurnOfMonth
from .cross_sectional import CrossSectionalMomentum, CrossSectionalReversal, LowVolatility
from .mean_reversion import RSIPullback
from .overlays import OVERLAYS, make_overlay
from .pairs import PairsTrading
from .trend import DonchianBreakout, TimeSeriesMomentum, VolatilityManaged

STRATEGIES: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (
        TimeSeriesMomentum,
        DonchianBreakout,
        CrossSectionalMomentum,
        RSIPullback,
        CrossSectionalReversal,
        PairsTrading,
        LowVolatility,
        VolatilityManaged,
        TurnOfMonth,
    )
}


def get_strategy(name: str) -> type[Strategy]:
    if name not in STRATEGIES:
        raise KeyError(f"Unknown strategy {name!r}. Available: {', '.join(STRATEGIES)}")
    return STRATEGIES[name]


__all__ = ["STRATEGIES", "OVERLAYS", "Strategy", "get_strategy", "make_overlay"]
