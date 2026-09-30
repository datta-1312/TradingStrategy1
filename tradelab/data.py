"""Market data: loading (Yahoo Finance, CSV, synthetic), alignment and quality checks.

All price panels are split/dividend adjusted and aligned to the benchmark's trading
calendar. Nothing here looks at future data; forward-filling is limited to short
gaps (exchange holidays) so that stale prices cannot masquerade as real quotes.
"""
from __future__ import annotations

import time
import warnings
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

FIELDS = ("open", "high", "low", "close", "volume")


@dataclass
class MarketData:
    """Aligned OHLCV panels (dates x assets) for the universe, plus the benchmark close."""

    open: pd.DataFrame
    high: pd.DataFrame
    low: pd.DataFrame
    close: pd.DataFrame
    volume: pd.DataFrame
    benchmark: pd.Series
    benchmark_name: str = "benchmark"
    source: str = "unknown"
    quality: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def assets(self) -> list[str]:
        return list(self.close.columns)

    @property
    def index(self) -> pd.DatetimeIndex:
        return self.close.index

    @property
    def has_volume(self) -> bool:
        return bool((self.volume.fillna(0) > 0).any().any())

    def returns(self) -> pd.DataFrame:
        return self.close.pct_change(fill_method=None)

    def benchmark_returns(self) -> pd.Series:
        return self.benchmark.pct_change(fill_method=None).fillna(0.0)

    def subset(self, assets: list[str]) -> "MarketData":
        return replace(
            self,
            open=self.open[assets], high=self.high[assets], low=self.low[assets],
            close=self.close[assets], volume=self.volume[assets],
        )

    def slice(self, start=None, end=None) -> "MarketData":
        sl = slice(start, end)
        return replace(
            self,
            open=self.open.loc[sl], high=self.high.loc[sl], low=self.low.loc[sl],
            close=self.close.loc[sl], volume=self.volume.loc[sl], benchmark=self.benchmark.loc[sl],
        )

    def truncate(self, end) -> "MarketData":
        """Data up to and including ``end`` - used to prove strategies do not peek ahead."""
        return self.slice(None, end)


# --------------------------------------------------------------------------- loading


def load_market_data(
    universe: list[str],
    benchmark: str,
    start: str,
    end: Optional[str] = None,
    source: str = "yahoo",
    csv_dir: Optional[str] = None,
    cache_dir: str = ".cache/marketdata",
    seed: int = 7,
    max_ffill: int = 5,
) -> MarketData:
    """Load, align and quality-check OHLCV data for ``universe`` and ``benchmark``."""
    tickers = list(dict.fromkeys([*universe, benchmark]))
    if source == "yahoo":
        raw = fetch_yahoo(tickers, start, end, cache_dir)
    elif source == "csv":
        if not csv_dir:
            raise ValueError("source='csv' requires csv_dir")
        raw = read_csv_dir(tickers, csv_dir)
    elif source == "synthetic":
        raw = generate_synthetic(tickers, start, end, seed=seed, benchmark=benchmark)
    else:
        raise ValueError(f"Unknown data source {source!r} (use yahoo, csv or synthetic)")
    return align_market_data(raw, universe, benchmark, start, end, max_ffill=max_ffill, source=source)


def align_market_data(
    raw: dict[str, pd.DataFrame],
    universe: list[str],
    benchmark: str,
    start: str,
    end: Optional[str] = None,
    max_ffill: int = 5,
    source: str = "unknown",
) -> MarketData:
    if benchmark not in raw or raw[benchmark].empty:
        raise ValueError(f"No data for benchmark {benchmark!r}")
    bench = raw[benchmark].sort_index()
    bench = bench.loc[pd.Timestamp(start): pd.Timestamp(end) if end else None]
    master = bench.index[bench["close"].notna()]
    if len(master) < 300:
        raise ValueError(f"Only {len(master)} benchmark observations - need at least 300 for research")

    available = [t for t in universe if t in raw and not raw[t].empty]
    missing = sorted(set(universe) - set(available))
    if missing:
        warnings.warn(f"No data for {missing}; they are dropped from the universe")
    if not available:
        raise ValueError("No universe members have data")

    panels: dict[str, dict[str, pd.Series]] = {f: {} for f in FIELDS}
    for t in available:
        df = raw[t].sort_index()
        df = df[~df.index.duplicated(keep="last")].reindex(master)
        close = df["close"].ffill(limit=max_ffill)
        # Do not invent history before the first real quote.
        first = df["close"].first_valid_index()
        if first is not None:
            close = close.where(close.index >= first)
        filled = close.notna() & df["close"].isna()
        for f in ("open", "high", "low"):
            s = df[f] if f in df else pd.Series(np.nan, index=master)
            panels[f][t] = s.where(~filled, close).fillna(close)
        panels["close"][t] = close
        vol = df["volume"] if "volume" in df else pd.Series(np.nan, index=master)
        panels["volume"][t] = vol.where(~filled, 0.0)

    frames = {f: pd.DataFrame(panels[f], index=master)[available].astype(float) for f in FIELDS}
    quality = data_quality_report(raw, frames["close"], [*available, benchmark], master)
    return MarketData(
        **frames,
        benchmark=bench["close"].reindex(master).ffill().astype(float),
        benchmark_name=benchmark,
        source=source,
        quality=quality,
    )


def _safe_name(ticker: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in ticker)


def fetch_yahoo(
    tickers: list[str], start: str, end: Optional[str], cache_dir: str, max_age_hours: float = 12.0
) -> dict[str, pd.DataFrame]:
    """Download adjusted daily OHLCV from Yahoo Finance with a simple on-disk cache."""
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise RuntimeError("Yahoo source needs yfinance: pip install yfinance") from exc

    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    todo: list[str] = []
    for t in tickers:
        path = cache / f"{_safe_name(t)}_{start}_{end or 'latest'}.csv"
        fresh = path.exists() and (end or time.time() - path.stat().st_mtime < max_age_hours * 3600)
        if fresh:
            out[t] = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            todo.append(t)

    if todo:
        df = yf.download(
            todo, start=start, end=end, auto_adjust=True, actions=False,
            progress=False, group_by="ticker", threads=True,
        )
        if df is None or df.empty:
            raise RuntimeError(
                f"Yahoo Finance returned no data for {todo}. Check tickers and network access."
            )
        for t in todo:
            if isinstance(df.columns, pd.MultiIndex):
                if t not in df.columns.get_level_values(0):
                    warnings.warn(f"Yahoo returned no data for {t}")
                    continue
                sub = df[t].copy()
            else:
                sub = df.copy()
            sub.columns = [str(c).lower() for c in sub.columns]
            sub = sub[[c for c in FIELDS if c in sub.columns]].dropna(subset=["close"])
            if sub.empty:
                warnings.warn(f"Yahoo returned no data for {t}")
                continue
            sub.index = pd.to_datetime(sub.index).tz_localize(None)
            sub.to_csv(cache / f"{_safe_name(t)}_{start}_{end or 'latest'}.csv")
            out[t] = sub
    return out


def read_csv_dir(tickers: list[str], csv_dir: str) -> dict[str, pd.DataFrame]:
    """Read ``<csv_dir>/<TICKER>.csv`` files with a date column and OHLC(V) columns.

    If an ``Adj Close`` column exists, OHLC are rescaled by adj_close / close so the
    whole bar is split/dividend adjusted.
    """
    out: dict[str, pd.DataFrame] = {}
    base = Path(csv_dir)
    for t in tickers:
        path = next((p for p in (base / f"{t}.csv", base / f"{_safe_name(t)}.csv") if p.exists()), None)
        if path is None:
            warnings.warn(f"No CSV for {t} in {csv_dir}")
            continue
        df = pd.read_csv(path)
        df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
        date_col = next(c for c in df.columns if c in ("date", "datetime", "timestamp", "time"))
        df.index = pd.to_datetime(df.pop(date_col)).dt.tz_localize(None)
        if "adj_close" in df.columns:
            ratio = df["adj_close"] / df["close"]
            for f in ("open", "high", "low"):
                if f in df:
                    df[f] = df[f] * ratio
            df["close"] = df["adj_close"]
        for f in ("open", "high", "low"):
            if f not in df:
                df[f] = df["close"]
        if "volume" not in df:
            df["volume"] = np.nan
        out[t] = df[list(FIELDS)].astype(float).sort_index()
    return out


# --------------------------------------------------------------------------- synthetic


def generate_synthetic(
    tickers: list[str],
    start: str,
    end: Optional[str] = None,
    seed: int = 7,
    benchmark: Optional[str] = None,
    cointegrated_pairs: int = 0,
    regime_switching: bool = True,
) -> dict[str, pd.DataFrame]:
    """Generate realistic-looking OHLCV panels with NO planted strategy edge.

    The market factor follows a persistent bull/sideways/bear Markov regime with
    fat-tailed shocks; assets load on the market plus a cluster ("sector") factor
    and idiosyncratic noise. Persistent regimes mechanically favour trend-following
    a little, so synthetic results are a pipeline demo, never market evidence.
    ``cointegrated_pairs`` > 0 plants mean-reverting pairs (for testing the pairs code).
    """
    idx = pd.bdate_range(start, end or pd.Timestamp.today().normalize())
    T = len(idx)
    rng = np.random.default_rng(seed)
    dt = 1 / 252

    def t_shock(size, df=5):
        x = rng.standard_t(df, size=size)
        return x / np.sqrt(df / (df - 2))

    # Market factor with Markov regimes: (annual drift, annual vol)
    params = np.array([[0.12, 0.13], [0.02, 0.17], [-0.28, 0.32]])
    P = np.array([[0.994, 0.004, 0.002], [0.006, 0.990, 0.004], [0.012, 0.010, 0.978]])
    states = np.zeros(T, dtype=int)
    if regime_switching:
        u = rng.random(T)
        for i in range(1, T):
            states[i] = np.searchsorted(np.cumsum(P[states[i - 1]]), u[i])
    mu, sig = params[states, 0], params[states, 1]
    mkt = mu * dt + sig * np.sqrt(dt) * t_shock(T)

    n_clusters = max(1, len(tickers) // 3)
    clusters = 0.10 * np.sqrt(dt) * t_shock((T, n_clusters))

    out: dict[str, pd.DataFrame] = {}
    planted = 0
    prev_log: Optional[np.ndarray] = None
    for k, t in enumerate(tickers):
        trng = np.random.default_rng(seed + zlib.crc32(t.encode()) % 100_000)
        if t == benchmark:
            r = mkt + 0.01 * np.sqrt(dt) * trng.standard_normal(T)
        elif planted < cointegrated_pairs and prev_log is not None and k % 2 == 1:
            # Mean-reverting spread around the previous asset (OU with ~4 day half-life).
            spread = np.zeros(T)
            for i in range(1, T):
                spread[i] = spread[i - 1] * (1 - 0.15) + 0.012 * trng.standard_normal()
            log_p = prev_log + spread
            r = np.diff(np.concatenate([[0.0], log_p]))
            r = np.expm1(r)
            r[0] = 0.0
            planted += 1
        else:
            beta = trng.uniform(0.6, 1.4)
            idio = trng.uniform(0.10, 0.30) * np.sqrt(dt) * t_shock(T, df=4)
            r = beta * mkt + clusters[:, k % n_clusters] + idio
        r = np.clip(r, -0.45, 0.6)
        log_r = np.log1p(r)
        prev_log = np.cumsum(log_r)
        sd = np.std(log_r) + 1e-9
        # Split each day into overnight and intraday legs; build a consistent OHLC bar.
        on = 0.3 * log_r + 0.2 * sd * trng.standard_normal(T)
        close = 100 * np.exp(np.cumsum(log_r))
        prev_close = np.concatenate([[100.0], close[:-1]])
        open_ = prev_close * np.exp(on)
        wick = np.abs(trng.normal(0, 0.5 * sd, (2, T)))
        high = np.maximum(open_, close) * np.exp(wick[0])
        low = np.minimum(open_, close) * np.exp(-wick[1])
        volume = trng.uniform(2e6, 2e7) * np.exp(trng.normal(0, 0.3, T)) * (1 + 20 * np.abs(r))
        out[t] = pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
        )
    return out


# --------------------------------------------------------------------------- quality


def data_quality_report(
    raw: dict[str, pd.DataFrame], aligned_close: pd.DataFrame, tickers: list[str], master: pd.DatetimeIndex
) -> pd.DataFrame:
    """Per-ticker data-quality facts: coverage, gaps, suspicious moves, stale prices."""
    rows = []
    for t in tickers:
        df = raw.get(t)
        if df is None or df.empty:
            rows.append({"ticker": t, "status": "missing"})
            continue
        c = df["close"].sort_index().loc[master[0]: master[-1]].dropna()
        if c.empty:
            rows.append({"ticker": t, "status": "no data in range"})
            continue
        r = c.pct_change().dropna()
        flat = (c.diff() == 0).astype(int)
        longest_flat = int(flat.groupby((flat == 0).cumsum()).sum().max()) if len(flat) else 0
        expected = master[(master >= c.index[0]) & (master <= c.index[-1])]
        n_missing = int(len(expected.difference(c.index)))
        flags = []
        if (c <= 0).any():
            flags.append("non-positive prices")
        if (r.abs() > 0.25).sum():
            flags.append(f"{int((r.abs() > 0.25).sum())} moves >25% (check splits/bad ticks)")
        if longest_flat >= 5:
            flags.append(f"{longest_flat}-day flat streak (stale quotes?)")
        if c.index[0] > master[0] + pd.Timedelta(days=30):
            flags.append(f"history starts {c.index[0].date()}")
        if c.index[-1] < master[-1] - pd.Timedelta(days=7):
            flags.append(f"stale: last quote {c.index[-1].date()}")
        if len(expected) and n_missing / len(expected) > 0.02:
            flags.append(f"{n_missing} missing sessions")
        rows.append({
            "ticker": t,
            "status": "ok" if not flags else "check",
            "first": c.index[0].date(),
            "last": c.index[-1].date(),
            "observations": len(c),
            "missing_sessions": n_missing,
            "max_abs_daily_move": float(r.abs().max()) if len(r) else np.nan,
            "flags": "; ".join(flags),
        })
    return pd.DataFrame(rows).set_index("ticker")
