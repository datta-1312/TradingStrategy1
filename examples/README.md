# Sample reports (synthetic data)

These reports were generated **on synthetic prices**, because the build environment could not reach market-data servers. They show the pipeline's output format and behaviour, including how it rejects weak strategies. They are **not** evidence about any real market. The tickers are only labels on generated series.

| Sample | Command |
|---|---|
| [`us-sectors_synthetic/REPORT.md`](us-sectors_synthetic/REPORT.md) | `python -m tradelab research --preset us-sectors --source synthetic --end 2025-09-30` |
| [`india-reliance_synthetic/REPORT.md`](india-reliance_synthetic/REPORT.md) | `python -m tradelab research --preset india-reliance --source synthetic --end 2025-09-30` |

For real results, drop `--source synthetic`; Yahoo Finance is the default source.

To keep the repository small, the per-grid `sensitivity_*.csv` and daily `walk_forward_returns.csv` files are left out here. A normal run writes them.
