"""Event-accurate portfolio backtester for daily target-weight strategies.

Conventions (these are what keep the results honest):

* A strategy emits *target weights* decided with information up to the close of
  day ``t``. The backtester never lets those weights earn day ``t``'s return.
* Execution modes:
    - ``next_open``  (default): trade at the next session's open. The carried book
      earns the overnight gap, the new book earns open->close.
    - ``next_close``: trade one full session later (a conservative "signal decay" test).
    - ``same_close``: trade at the signal day's close (market-on-close; optimistic).
* Between rebalances positions *drift* with prices; a trade happens only when the
  target changes, so buy-and-hold style strategies are not charged phantom turnover.
* Costs: per-side commission/slippage/taxes on traded notional, square-root market
  impact from ADV and volatility, short borrow fees, and financing on leverage.
  Idle cash earns the configured risk-free rate.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .costs import CostModel
from .data import MarketData

EXECUTION_MODES = ("next_open", "next_close", "same_close")


@dataclass
class BacktestResult:
    label: str
    returns: pd.Series          # net daily returns
    gross_returns: pd.Series    # before trading costs and carry
    cost: pd.Series             # trading cost drag per bar (fraction of equity)
    turnover: pd.Series         # sum of |weight change| per bar
    weights: pd.DataFrame       # exposure that earned each bar's return
    contributions: pd.DataFrame  # per-asset net P&L per bar (fraction of equity, approx.)
    participation: pd.Series    # max share of ADV traded in any asset per bar
    execution: str
    meta: dict = field(default_factory=dict)
    _trades: pd.DataFrame | None = None

    @property
    def equity(self) -> pd.Series:
        return (1 + self.returns).cumprod()

    @property
    def trades(self) -> pd.DataFrame:
        if self._trades is None:
            self._trades = extract_trades(self.weights, self.contributions)
        return self._trades

    def window(self, start=None, end=None) -> pd.Series:
        return self.returns.loc[start:end]


def run_backtest(
    targets: pd.DataFrame,
    data: MarketData,
    costs: CostModel,
    execution: str = "next_open",
    capital: float = 1_000_000.0,
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
    min_trade_weight: float = 0.0,
    label: str = "",
) -> BacktestResult:
    """Simulate ``targets`` (dates x assets weights) on ``data`` net of all costs."""
    if execution not in EXECUTION_MODES:
        raise ValueError(f"execution must be one of {EXECUTION_MODES}")
    idx = data.index
    assets = data.assets
    tgt = targets.reindex(index=idx, columns=assets).to_numpy(dtype=float)
    close = data.close.to_numpy(dtype=float)
    opn = data.open.to_numpy(dtype=float)
    T, N = close.shape

    # Can only hold what has a price at the decision close.
    tgt = np.where(np.isfinite(tgt) & np.isfinite(close), tgt, 0.0)
    changed = np.zeros(T, dtype=bool)
    changed[0] = np.abs(tgt[0]).sum() > 0
    changed[1:] = (np.abs(np.diff(tgt, axis=0)) > 1e-12).any(axis=1)

    with np.errstate(invalid="ignore", divide="ignore"):
        prev_close = np.vstack([np.full((1, N), np.nan), close[:-1]])
        r_cc = np.nan_to_num(close / prev_close - 1.0, nan=0.0, posinf=0.0, neginf=0.0)
        r_on = np.nan_to_num(opn / prev_close - 1.0, nan=0.0, posinf=0.0, neginf=0.0)
        r_id = np.nan_to_num(close / opn - 1.0, nan=0.0, posinf=0.0, neginf=0.0)

    # Inputs for the square-root impact model and liquidity checks (strictly lagged).
    rets = pd.DataFrame(r_cc, index=idx, columns=assets)
    sigma = rets.rolling(21, min_periods=10).std().shift(1).fillna(0.02).to_numpy()
    use_impact = costs.impact_coef > 0 and data.has_volume
    if data.has_volume:
        dollar_vol = (data.close * data.volume.fillna(0)).rolling(20, min_periods=5).mean().shift(1)
        adv = dollar_vol.to_numpy(dtype=float)  # NaN until 5 sessions of history: no impact estimate yet
    else:
        adv = np.full((T, N), np.nan)

    rf_d = risk_free_rate / periods_per_year
    fin_d = costs.financing_spread_bps / 1e4 / periods_per_year
    borrow_d = costs.borrow_bps / 1e4 / periods_per_year
    buy_rate, sell_rate = costs.buy_rate, costs.sell_rate

    ret = np.zeros(T)
    gross = np.zeros(T)
    cost_arr = np.zeros(T)
    turn_arr = np.zeros(T)
    part_arr = np.zeros(T)
    exposure = np.zeros((T, N))
    contrib = np.zeros((T, N))
    cost_asset = np.zeros((T, N))

    w = np.zeros(N)
    equity = 1.0

    def trade(w_now: np.ndarray, target: np.ndarray, t: int) -> tuple[np.ndarray, float]:
        delta = target - w_now
        if min_trade_weight > 0:
            delta = np.where((np.abs(delta) < min_trade_weight) & (target != 0), 0.0, delta)
        absd = np.abs(delta)
        if absd.sum() == 0:
            return w_now, 0.0
        per_asset = np.where(delta > 0, delta * buy_rate, -delta * sell_rate)
        notional = absd * equity * capital
        with np.errstate(invalid="ignore", divide="ignore"):
            part = np.where(adv[t] > 0, notional / adv[t], np.nan)
        if use_impact:
            per_asset = per_asset + absd * costs.impact_coef * sigma[t] * np.sqrt(np.nan_to_num(part))
        part_arr[t] = np.nanmax(part) if np.isfinite(part).any() else 0.0
        turn_arr[t] += absd.sum()
        cost_asset[t] += per_asset
        return w_now + delta, float(per_asset.sum())

    if execution == "same_close" and changed[0]:
        w, c = trade(w, tgt[0], 0)
        equity *= 1 - c
        ret[0] = -c
        cost_arr[0] = c

    for t in range(1, T):
        growth = 1.0
        c_total = 0.0
        if execution == "next_open":
            g_on = float(w @ r_on[t])
            contrib[t] += w * r_on[t]
            if 1 + g_on > 0:
                w = w * (1 + r_on[t]) / (1 + g_on)
            growth *= 1 + g_on
            if changed[t - 1]:
                w, c = trade(w, tgt[t - 1], t)
                growth *= 1 - c
                c_total += c
            exposure[t] = w
            g_id = float(w @ r_id[t])
            contrib[t] += w * r_id[t]
            if 1 + g_id > 0:
                w = w * (1 + r_id[t]) / (1 + g_id)
            growth *= 1 + g_id
        else:
            exposure[t] = w
            g = float(w @ r_cc[t])
            contrib[t] += w * r_cc[t]
            if 1 + g > 0:
                w = w * (1 + r_cc[t]) / (1 + g)
            growth *= 1 + g
            fire = changed[t - 1] if execution == "next_close" else changed[t]
            if fire:
                w, c = trade(w, tgt[t - 1] if execution == "next_close" else tgt[t], t)
                growth *= 1 - c
                c_total += c
        gross[t] = growth / (1 - c_total) - 1 if c_total < 1 else -1.0

        # Carry: idle cash earns rf; leverage pays rf + spread; shorts pay borrow.
        cash = 1.0 - w.sum()
        carry = cash * rf_d if cash >= 0 else cash * (rf_d + fin_d)
        carry -= borrow_d * np.clip(-w, 0, None).sum()
        growth *= 1 + carry
        cost_arr[t] = c_total

        ret[t] = growth - 1.0
        equity *= growth
        if equity <= 0:  # wiped out - stop trading
            ret[t] = -1.0
            ret[t + 1:] = 0.0
            break

    contrib -= _attribute_costs(cost_asset, exposure)
    return BacktestResult(
        label=label,
        returns=pd.Series(ret, index=idx, name=label),
        gross_returns=pd.Series(gross, index=idx),
        cost=pd.Series(cost_arr, index=idx),
        turnover=pd.Series(turn_arr, index=idx),
        weights=pd.DataFrame(exposure, index=idx, columns=assets),
        contributions=pd.DataFrame(contrib, index=idx, columns=assets),
        participation=pd.Series(part_arr, index=idx),
        execution=execution,
        meta={"costs": costs.name, "capital": capital},
    )


def _attribute_costs(cost_asset: np.ndarray, exposure: np.ndarray) -> np.ndarray:
    """Move each trading cost onto a bar where the traded position is live.

    Close-execution modes pay for an entry on the bar *before* the exposure starts
    (shift forward); open execution pays for an exit on the bar *after* it ends
    (shift back). Costs on bars with live exposure stay put, so totals are preserved.
    """
    live = np.abs(exposure) > 1e-12
    dead = ~live & (cost_asset != 0)
    out = np.where(live, cost_asset, 0.0)

    to_next = np.zeros_like(live)
    to_next[:-1] = dead[:-1] & live[1:]
    out[1:] += np.where(to_next[:-1], cost_asset[:-1], 0.0)

    to_prev = np.zeros_like(live)
    to_prev[1:] = dead[1:] & ~to_next[1:] & live[:-1]
    out[:-1] += np.where(to_prev[1:], cost_asset[1:], 0.0)

    out += np.where(dead & ~to_next & ~to_prev, cost_asset, 0.0)
    return out


def extract_trades(weights: pd.DataFrame, contributions: pd.DataFrame) -> pd.DataFrame:
    """Position spells per asset: a trade opens when exposure turns non-zero or flips sign."""
    rows = []
    w = weights.to_numpy()
    c = contributions.to_numpy()
    idx = weights.index
    for j, asset in enumerate(weights.columns):
        sign = np.sign(np.where(np.abs(w[:, j]) > 1e-9, w[:, j], 0.0))
        if not sign.any():
            continue
        boundaries = np.flatnonzero(np.diff(np.concatenate([[0], sign, [0]])) != 0)
        for a, b in zip(boundaries[:-1], boundaries[1:]):
            if sign[a] == 0:
                continue
            rows.append({
                "asset": asset,
                "direction": "long" if sign[a] > 0 else "short",
                "entry": idx[a],
                "exit": idx[b - 1],
                "bars": int(b - a),
                "avg_weight": float(np.abs(w[a:b, j]).mean()),
                "pnl": float(c[a:b, j].sum()),
            })
    return pd.DataFrame(rows, columns=["asset", "direction", "entry", "exit", "bars", "avg_weight", "pnl"])


def buy_and_hold(data: MarketData, costs: CostModel, execution: str = "next_open", **kw) -> BacktestResult:
    """Equal-weight buy-and-hold of the universe (bought once, never rebalanced)."""
    tgt = pd.DataFrame(0.0, index=data.index, columns=data.assets)
    valid = data.close.notna().all(axis=1)
    first = valid.idxmax() if valid.any() else data.index[0]
    tgt.loc[first:, :] = 1.0 / len(data.assets)
    return run_backtest(tgt, data, costs, execution=execution, label="equal_weight_buy_hold", **kw)
