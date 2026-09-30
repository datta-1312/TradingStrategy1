"""Steps 1-2 of the research process: market overview and opportunity scan.

Outputs are split into *facts* (computed numbers) and *interpretation* (heuristic
readings of those numbers), so the report never blurs the two.
"""
from __future__ import annotations

import math
from typing import Optional

import numpy as np
import pandas as pd

from .data import MarketData
from .strategies.base import rsi
from .validation import market_regimes

MACRO_TICKERS = {
    "^VIX": "VIX (S&P 500 implied vol)",
    "^TNX": "US 10y Treasury yield (x10)",
    "DX-Y.NYB": "US Dollar index",
    "CL=F": "WTI crude oil",
    "GC=F": "Gold",
}


def _ret(s: pd.Series, n: int) -> float:
    s = s.dropna()
    return float(s.iloc[-1] / s.iloc[-n - 1] - 1) if len(s) > n else float("nan")


def _avg_pairwise_corr(rets: pd.DataFrame) -> float:
    c = rets.corr().to_numpy()
    n = c.shape[0]
    if n < 2:
        return float("nan")
    return float((np.nansum(c) - np.trace(c)) / (n * (n - 1)))


def market_overview(data: MarketData, periods: int = 252) -> dict:
    b = data.benchmark.dropna()
    br = b.pct_change()
    sma50, sma200 = b.rolling(50).mean(), b.rolling(200).mean()
    vol21 = br.rolling(21).std() * math.sqrt(periods)
    regimes = market_regimes(b, periods)
    rets = data.returns()

    facts = {
        "as_of": b.index[-1].date(),
        "benchmark": data.benchmark_name,
        "return_1m": _ret(b, 21),
        "return_3m": _ret(b, 63),
        "return_6m": _ret(b, 126),
        "return_12m": _ret(b, 252),
        "distance_from_200d_sma": float(b.iloc[-1] / sma200.iloc[-1] - 1),
        "sma50_above_sma200": bool(sma50.iloc[-1] > sma200.iloc[-1]),
        "drawdown_from_high": float(b.iloc[-1] / b.cummax().iloc[-1] - 1),
        "realised_vol_21d": float(vol21.iloc[-1]),
        "realised_vol_3y_median": float(vol21.iloc[-756:].median()),
        "vol_percentile_3y": float((vol21.iloc[-756:] <= vol21.iloc[-1]).mean()),
        "trend_regime": regimes["trend"].iloc[-1],
        "vol_regime": regimes["vol"].iloc[-1],
    }
    close = data.close
    if len(data.assets) > 1:
        above200 = close.iloc[-1] > close.rolling(200).mean().iloc[-1]
        above50 = close.iloc[-1] > close.rolling(50).mean().iloc[-1]
        corr_now = _avg_pairwise_corr(rets.iloc[-63:])
        hist = [_avg_pairwise_corr(rets.iloc[i - 63: i]) for i in range(len(rets) - 756, len(rets), 21) if i > 63]
        facts.update({
            "breadth_above_200d": float(above200.mean()),
            "breadth_above_50d": float(above50.mean()),
            "avg_pairwise_corr_63d": corr_now,
            "corr_percentile_3y": float(np.mean(np.array(hist) <= corr_now)) if hist else float("nan"),
            "dispersion_3m": float((close.iloc[-1] / close.iloc[-64] - 1).std()) if len(close) > 64 else float("nan"),
        })
    return {"facts": facts, "interpretation": interpret_overview(facts), "regime_history": regimes}


def interpret_overview(f: dict) -> list[str]:
    """Heuristic reading of the facts. Labelled as interpretation in the report."""
    out = []
    trend = f["trend_regime"]
    if trend == "bull":
        out.append("The benchmark is in an uptrend (above a rising 200-day average). Historically, trend "
                   "following and relative-strength rotation have their best conditions in persistent trends, "
                   "and buying dips within the trend has favourable context.")
    elif trend == "bear":
        out.append("The benchmark is in a downtrend (below a falling 200-day average). Long-only dip buying and "
                   "momentum have poor context; capital preservation (trend filters, defensive assets) matters most.")
    else:
        out.append("Trend is ambiguous (price and the 200-day average disagree). Range-bound conditions tend to "
                   "cause whipsaws for trend rules and favour mean reversion.")
    if f["vol_percentile_3y"] >= 0.8:
        out.append(f"Volatility is elevated (21-day vol at the {f['vol_percentile_3y']:.0%} percentile of 3 years). "
                   "Volatility-scaled sizing cuts exposure here, and short-term reversal effects are typically stronger.")
    elif f["vol_percentile_3y"] <= 0.2:
        out.append("Volatility is unusually low. Calm regimes can end abruptly; vol-targeted books are near full "
                   "size and therefore most exposed to a volatility shock.")
    if f["drawdown_from_high"] < -0.15:
        out.append(f"The benchmark is {-f['drawdown_from_high']:.0%} below its high, which is correction or "
                   "bear-market territory.")
    if "avg_pairwise_corr_63d" in f and np.isfinite(f.get("corr_percentile_3y", np.nan)):
        if f["corr_percentile_3y"] >= 0.75:
            out.append("Cross-asset correlation is high versus its 3-year history, so the market is macro-driven. "
                       "There is less dispersion for relative-value strategies to harvest and less diversification "
                       "benefit.")
        elif f["corr_percentile_3y"] <= 0.25:
            out.append("Correlation is low versus history, a 'stock-picker's market' in which relative strength "
                       "and pairs strategies have more dispersion to work with.")
    if "breadth_above_200d" in f:
        b = f["breadth_above_200d"]
        if trend == "bull" and b < 0.5:
            out.append(f"Narrow leadership: only {b:.0%} of the universe is above its 200-day average while the "
                       "benchmark is rising. Narrow rallies are more fragile.")
        elif b > 0.8:
            out.append(f"Broad participation: {b:.0%} of the universe is above its 200-day average.")
    return out


def opportunity_scan(data: MarketData, periods: int = 252) -> pd.DataFrame:
    """Per-asset quantitative signals: momentum, relative strength, volatility, unusual moves, volume."""
    close = data.close
    b = data.benchmark
    rets = close.pct_change(fill_method=None)
    rows = []
    r21 = close / close.shift(21) - 1
    rsi14 = rsi(close, 14)
    for a in data.assets:
        c = close[a].dropna()
        if len(c) < 260:
            continue
        hist = r21[a].dropna()
        z = (hist.iloc[-1] - hist.mean()) / hist.std() if hist.std() > 0 else 0.0
        vol = data.volume[a]
        vol_trend = float(vol.iloc[-20:].mean() / vol.iloc[-120:].mean()) if vol.iloc[-120:].mean() > 0 else float("nan")
        row = {
            "asset": a,
            "ret_1m": _ret(c, 21), "ret_3m": _ret(c, 63), "ret_6m": _ret(c, 126), "ret_12m": _ret(c, 252),
            "rel_strength_6m": _ret(c, 126) - _ret(b, 126),
            "vol_3m": float(rets[a].iloc[-63:].std() * math.sqrt(periods)),
            "dist_52w_high": float(c.iloc[-1] / c.iloc[-252:].max() - 1),
            "rsi14": float(rsi14[a].iloc[-1]),
            "above_200d": bool(c.iloc[-1] > c.iloc[-200:].mean()),
            "z_1m_move": float(z),
            "volume_trend": vol_trend,
        }
        flags = []
        if z >= 2:
            flags.append("unusual strength")
        if z <= -2:
            flags.append("unusual weakness")
        if np.isfinite(vol_trend) and vol_trend >= 1.5:
            flags.append("volume surge")
        if row["rsi14"] < 30 and row["above_200d"]:
            flags.append("oversold in uptrend")
        if row["rsi14"] > 75:
            flags.append("overbought")
        if row["dist_52w_high"] > -0.02:
            flags.append("at 52w high")
        if not row["above_200d"] and row["dist_52w_high"] < -0.25:
            flags.append("broken trend")
        row["flags"] = ", ".join(flags)
        rows.append(row)
    df = pd.DataFrame(rows).set_index("asset") if rows else pd.DataFrame()
    if len(df) > 1:
        df["momentum_rank"] = df[["ret_3m", "ret_6m", "ret_12m"]].rank(ascending=False).mean(axis=1).rank().astype(int)
        df = df.sort_values("momentum_rank")
    return df


def macro_snapshot(frames: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for t, label in MACRO_TICKERS.items():
        df = frames.get(t)
        if df is None or df.empty:
            continue
        c = df["close"].dropna()
        rows.append({"series": label, "ticker": t, "last": float(c.iloc[-1]), "change_3m": _ret(c, 63),
                     "change_12m": _ret(c, 252), "as_of": c.index[-1].date()})
    return pd.DataFrame(rows)


def fetch_macro(start: str, cache_dir: str) -> Optional[pd.DataFrame]:
    """Best-effort macro context from Yahoo Finance; returns None if unavailable."""
    try:
        from .data import fetch_yahoo
        return macro_snapshot(fetch_yahoo(list(MACRO_TICKERS), start, None, cache_dir))
    except Exception:  # noqa: BLE001 - macro context is optional
        return None
