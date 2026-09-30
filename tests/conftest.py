import numpy as np
import pandas as pd
import pytest

from tradelab.data import MarketData, align_market_data, generate_synthetic


def make_data(prices: dict[str, list[float]], opens: dict[str, list[float]] | None = None,
              volume: float = 1e6) -> MarketData:
    """Tiny hand-built MarketData for exact accounting tests."""
    n = len(next(iter(prices.values())))
    idx = pd.bdate_range("2020-01-01", periods=n)
    close = pd.DataFrame(prices, index=idx, dtype=float)
    opn = pd.DataFrame(opens, index=idx, dtype=float) if opens else close.shift(1).fillna(close)
    return MarketData(
        open=opn, high=np.maximum(opn, close), low=np.minimum(opn, close), close=close,
        volume=pd.DataFrame(volume, index=idx, columns=close.columns),
        benchmark=close.iloc[:, 0], benchmark_name="BENCH", source="test",
    )


@pytest.fixture(scope="session")
def synth() -> MarketData:
    tickers = ["A", "B", "C", "D", "E", "F", "BENCH"]
    raw = generate_synthetic(tickers, "2012-01-01", "2019-12-31", seed=11, benchmark="BENCH")
    return align_market_data(raw, tickers[:-1], "BENCH", "2012-01-01", "2019-12-31", source="synthetic")
