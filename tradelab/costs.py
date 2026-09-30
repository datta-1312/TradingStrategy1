"""Transaction-cost model: commissions, spread/slippage, taxes, market impact, carry.

Per-side costs are charged on traded notional. Market impact follows the widely used
square-root law: cost ~= Y * sigma_daily * sqrt(traded_value / ADV). Short positions
pay a borrow fee and leverage pays a financing spread over the cash rate.

Preset numbers are deliberately conservative approximations - verify them against
your broker's schedule before relying on any result.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace


@dataclass(frozen=True)
class CostModel:
    name: str = "custom"
    commission_bps: float = 1.0          # broker + exchange fees, per side
    slippage_bps: float = 5.0            # half-spread + slippage, per side
    buy_tax_bps: float = 0.0             # e.g. stamp duty / STT on purchases
    sell_tax_bps: float = 0.0            # e.g. STT / SEC fee on sales
    impact_coef: float = 0.5             # Y in Y * sigma * sqrt(Q / ADV); 0 disables impact
    borrow_bps: float = 50.0             # annual fee on short notional
    financing_spread_bps: float = 150.0  # annual spread over the cash rate paid on leverage

    @property
    def buy_rate(self) -> float:
        return (self.commission_bps + self.slippage_bps + self.buy_tax_bps) / 1e4

    @property
    def sell_rate(self) -> float:
        return (self.commission_bps + self.slippage_bps + self.sell_tax_bps) / 1e4

    def scaled(self, k: float) -> "CostModel":
        """Every cost multiplied by ``k`` - used for cost stress tests."""
        return replace(
            self,
            name=f"{self.name} x{k:g}",
            commission_bps=self.commission_bps * k,
            slippage_bps=self.slippage_bps * k,
            buy_tax_bps=self.buy_tax_bps * k,
            sell_tax_bps=self.sell_tax_bps * k,
            impact_coef=self.impact_coef * k,
            borrow_bps=self.borrow_bps * k,
            financing_spread_bps=self.financing_spread_bps * k,
        )

    def describe(self) -> str:
        return (
            f"{self.name}: buy {self.buy_rate * 1e4:.1f} bps / sell {self.sell_rate * 1e4:.1f} bps per side "
            f"(commission {self.commission_bps:g}, slippage {self.slippage_bps:g}, taxes "
            f"{self.buy_tax_bps:g}/{self.sell_tax_bps:g}), sqrt-impact Y={self.impact_coef:g}, "
            f"borrow {self.borrow_bps:g} bps/yr, leverage financing +{self.financing_spread_bps:g} bps/yr"
        )

    def to_dict(self) -> dict:
        return asdict(self)


COST_PRESETS: dict[str, CostModel] = {
    "zero": CostModel("zero", 0, 0, 0, 0, 0, 0, 0),
    "us_etf": CostModel("us_etf", commission_bps=0.5, slippage_bps=2.0, borrow_bps=30),
    "us_equity": CostModel("us_equity", commission_bps=1.0, slippage_bps=5.0, borrow_bps=50),
    "us_small_cap": CostModel("us_small_cap", commission_bps=2.0, slippage_bps=15.0, impact_coef=1.0,
                              borrow_bps=250),
    # India cash-delivery: STT 10 bps both sides, stamp duty 1.5 bps on buys; shorting overnight
    # needs SLB/F&O, hence the high borrow proxy.
    "india_equity": CostModel("india_equity", commission_bps=0.5, slippage_bps=5.0, buy_tax_bps=11.5,
                              sell_tax_bps=10.0, borrow_bps=300),
    "crypto": CostModel("crypto", commission_bps=10.0, slippage_bps=5.0, impact_coef=0.8, borrow_bps=800,
                        financing_spread_bps=500),
}


def get_cost_model(name: str) -> CostModel:
    if name not in COST_PRESETS:
        raise KeyError(f"Unknown cost preset {name!r}. Available: {', '.join(sorted(COST_PRESETS))}")
    return COST_PRESETS[name]
