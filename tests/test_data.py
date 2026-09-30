import sys
import types

import numpy as np
import pandas as pd
import pytest

from tradelab.data import align_market_data, fetch_yahoo, generate_synthetic, read_csv_dir


def test_csv_reader_adjusts_ohlc(tmp_path):
    idx = pd.bdate_range("2020-01-01", periods=3)
    pd.DataFrame({"Date": idx, "Open": [10, 11, 12], "High": [11, 12, 13], "Low": [9, 10, 11],
                  "Close": [10, 11, 12], "Adj Close": [5, 5.5, 6], "Volume": [100, 100, 100]}).to_csv(
        tmp_path / "XYZ.csv", index=False)
    out = read_csv_dir(["XYZ"], str(tmp_path))["XYZ"]
    assert out["close"].tolist() == [5, 5.5, 6]
    assert out["open"].iloc[1] == pytest.approx(5.5)


def test_fetch_yahoo_parses_multiindex(monkeypatch, tmp_path):
    idx = pd.bdate_range("2020-01-01", periods=4)
    cols = pd.MultiIndex.from_product([["AAA", "BBB"], ["Open", "High", "Low", "Close", "Volume"]])
    frame = pd.DataFrame(np.arange(40, dtype=float).reshape(4, 10) + 1, index=idx, columns=cols)
    fake = types.SimpleNamespace(download=lambda *a, **k: frame)
    monkeypatch.setitem(sys.modules, "yfinance", fake)
    out = fetch_yahoo(["AAA", "BBB"], "2020-01-01", "2020-02-01", str(tmp_path))
    assert set(out) == {"AAA", "BBB"}
    assert list(out["AAA"].columns) == ["open", "high", "low", "close", "volume"]
    # second call is served from the cache without downloading
    monkeypatch.setitem(sys.modules, "yfinance", types.SimpleNamespace(download=None))
    again = fetch_yahoo(["AAA"], "2020-01-01", "2020-02-01", str(tmp_path))
    assert again["AAA"]["close"].tolist() == out["AAA"]["close"].tolist()


def test_align_keeps_pre_listing_history_empty():
    raw = generate_synthetic(["OLD", "NEW", "B"], "2015-01-01", "2018-12-31", seed=1, benchmark="B")
    raw["NEW"] = raw["NEW"].loc["2017-01-01":]
    d = align_market_data(raw, ["OLD", "NEW"], "B", "2015-01-01", "2018-12-31")
    assert d.close["NEW"].loc[:"2016-12-30"].isna().all()
    assert d.close["NEW"].loc["2017-01-03":].notna().all()
    assert "history starts" in d.quality.loc["NEW", "flags"]
