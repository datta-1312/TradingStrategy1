"""Research configuration: universe presets, evaluation criteria and run settings.

Every threshold used to accept or reject a strategy lives here so that the
acceptance bar is explicit, versioned and easy to audit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Criteria:
    """Quality bar a strategy must clear (mirrors the brief's <evaluation_criteria>).

    Hard checks decide REJECT vs. not; evidence checks decide VALIDATED vs. PROMISING.
    """

    min_sharpe: float = 0.5              # hard floor on walk-forward (out-of-sample) Sharpe
    preferred_sharpe: float = 1.0        # "Sharpe > 1.0 preferred" (scored, not required)
    max_drawdown: float = 0.30           # "acceptable drawdown (e.g. < 30%)"
    min_positive_years: float = 0.55     # share of calendar years with positive return
    min_cost_resilience: float = 0.5     # Sharpe at 2x costs must keep >= 50% of base Sharpe
    min_param_robustness: float = 0.5    # median/best Sharpe ratio across the parameter grid
    min_deflated_sharpe: float = 0.90    # probability true Sharpe > 0 after multiple-testing haircut
    max_asset_pnl_share: float = 0.60    # no single asset may drive > 60% of total P&L
    max_adv_participation: float = 0.05  # never trade more than 5% of average daily volume

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ResearchConfig:
    """Everything needed to reproduce one research run."""

    name: str
    universe: list[str]
    benchmark: str
    start: str = "2005-01-01"
    end: Optional[str] = None
    source: str = "yahoo"                # yahoo | csv | synthetic
    csv_dir: Optional[str] = None
    cache_dir: str = ".cache/marketdata"
    cost_preset: str = "us_equity"
    capital: float = 1_000_000.0
    periods_per_year: int = 252
    risk_free_rate: float = 0.0          # annual; cash earns it and Sharpe is measured in excess of it
    execution: str = "next_open"         # next_open | next_close | same_close
    oos_fraction: float = 0.30           # hold-out share for the simple IS/OOS split
    wf_train_years: float = 5.0          # walk-forward training window
    wf_test_years: float = 1.0           # walk-forward test window (step size)
    wf_anchored: bool = False            # False = rolling window, True = expanding window
    strategies: Optional[list[str]] = None  # None = every applicable strategy
    max_grid: int = 24                   # cap on parameter combinations per strategy
    iterate: bool = True                 # run the overlay iteration round on weak strategies
    top_n: int = 5                       # how many strategies to recommend at most
    output_dir: str = "reports"
    seed: int = 7
    notes: list[str] = field(default_factory=list)
    criteria: Criteria = field(default_factory=Criteria)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["criteria"] = self.criteria.to_dict()
        return d


US_SECTORS = ["XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY"]
US_MULTI_ASSET = ["SPY", "QQQ", "IWM", "EFA", "EEM", "TLT", "IEF", "GLD", "DBC", "VNQ"]
US_MEGACAPS = ["AAPL", "MSFT", "AMZN", "GOOGL", "JPM", "XOM", "JNJ", "PG", "KO", "WMT", "HD", "UNH"]
INDIA_LEADERS = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "LT.NS",
]
CRYPTO_MAJORS = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD"]

PRESETS: dict[str, dict] = {
    "us-sectors": dict(
        universe=US_SECTORS, benchmark="SPY", cost_preset="us_etf", start="2000-01-01",
        notes=["Original nine SPDR sector ETFs (XLC and XLRE excluded for history length)."],
    ),
    "us-multi-asset": dict(
        universe=US_MULTI_ASSET, benchmark="SPY", cost_preset="us_etf", start="2007-01-01",
        notes=["Cross-asset ETF universe: equities, bonds, gold, commodities, REITs."],
    ),
    "us-megacaps": dict(
        universe=US_MEGACAPS, benchmark="SPY", cost_preset="us_equity", start="2006-01-01",
        notes=[
            "SURVIVORSHIP BIAS: today's large caps were picked with hindsight; "
            "results overstate what was achievable ex-ante.",
        ],
    ),
    "india-reliance": dict(
        universe=["RELIANCE.NS"], benchmark="^NSEI", cost_preset="india_equity", start="2008-01-01",
        notes=[
            "Single-stock study: cross-sectional and pairs strategies do not apply; "
            "concentration risk is inherent.",
        ],
    ),
    "india-leaders": dict(
        universe=INDIA_LEADERS, benchmark="^NSEI", cost_preset="india_equity", start="2008-01-01",
        notes=[
            "SURVIVORSHIP BIAS: current NIFTY leaders chosen with hindsight.",
        ],
    ),
    "crypto": dict(
        universe=CRYPTO_MAJORS, benchmark="BTC-USD", cost_preset="crypto", start="2018-01-01",
        periods_per_year=365, wf_train_years=3.0,
        notes=["24/7 market: annualised with 365 periods; exchange/custody risk not modelled."],
    ),
}


def config_from_preset(preset: str, **overrides) -> ResearchConfig:
    """Build a ResearchConfig from a named preset, applying keyword overrides."""
    if preset not in PRESETS:
        raise KeyError(f"Unknown preset {preset!r}. Available: {', '.join(sorted(PRESETS))}")
    params = dict(PRESETS[preset])
    params.update({k: v for k, v in overrides.items() if v is not None})
    return ResearchConfig(name=preset, **params)
