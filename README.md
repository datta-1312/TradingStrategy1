# TradingStrategy1: `tradelab`

A skeptical, systematic **trading-strategy research pipeline** that implements the research brief in [`PROMPT.md`](PROMPT.md). Give it a market (US sectors, a multi-asset ETF set, a single stock like RELIANCE, crypto, or your own tickers) and it will:

1. analyse the current market regime,
2. scan the universe for opportunities,
3. propose fundamentally different strategies,
4. implement them,
5. backtest them with realistic costs,
6. validate them out of sample,
7. try to repair weak ones,
8. write a ranked report.

The report says plainly when **no strategy is good enough to trade**.

```bash
pip install -r requirements.txt
python -m tradelab research --preset us-sectors            # live Yahoo Finance data
python -m tradelab research --preset india-reliance        # single stock (RELIANCE.NS vs NIFTY 50)
python -m tradelab research --universe AAPL,MSFT,NVDA,AMD --benchmark QQQ --costs us_equity
python -m tradelab research --preset us-sectors --source synthetic   # offline demo, no network
```

Each run writes `reports/<name>_<date>/REPORT.md` with charts in `charts/` and CSV/JSON in `results/`.

**Sample output:** [`examples/us-sectors_synthetic/REPORT.md`](examples/us-sectors_synthetic/REPORT.md) and [`examples/india-reliance_synthetic/REPORT.md`](examples/india-reliance_synthetic/REPORT.md). These samples use **synthetic prices** because the build environment could not reach market-data servers. They show what the report looks like and say nothing about real markets. Run the commands above on a machine with internet access for real results.

---

## How the brief maps to the code

| Brief section | Where it lives |
|---|---|
| 1. Market & sector analysis (regime, volatility, breadth, correlation, macro) | [`tradelab/market.py`](tradelab/market.py): `market_overview`, `fetch_macro` |
| 2. Opportunity scan (relative strength, momentum, unusual moves, volume) | [`tradelab/market.py`](tradelab/market.py): `opportunity_scan` |
| 3. Strategy ideation: 3-10 *different* strategies, each with thesis & invalidation | [`tradelab/strategies/`](tradelab/strategies) (9 strategies) |
| 4. Implementation (clean, documented, causal) | same; every strategy passes a look-ahead truncation test |
| 5. Backtesting with realistic costs, slippage, sizing, liquidity | [`tradelab/backtest.py`](tradelab/backtest.py), [`tradelab/costs.py`](tradelab/costs.py) |
| 6. Evaluation: IS/OOS, walk-forward, metrics, benchmarks | [`tradelab/validation.py`](tradelab/validation.py), [`tradelab/metrics.py`](tradelab/metrics.py) |
| 7. Iteration: improve or discard | `ResearchPipeline.iterate` + [`strategies/overlays.py`](tradelab/strategies/overlays.py) |
| 8. Final selection, ranking, deployment, risks | [`tradelab/evaluation.py`](tradelab/evaluation.py), [`tradelab/report.py`](tradelab/report.py) |
| `<evaluation_criteria>` | `Criteria` in [`tradelab/config.py`](tradelab/config.py) (explicit thresholds) |
| `<output_format>` sections 1-8 + `<final_check>` | the generated `REPORT.md` (same headings, in order) |

Orchestration: [`tradelab/research.py`](tradelab/research.py). CLI: [`tradelab/cli.py`](tradelab/cli.py).

## Strategy library

| Name | Family | Why an edge might exist | Min assets |
|---|---|---|---|
| `trend_tsmom` | Time-series momentum, vol-targeted | Slow information diffusion, herding, persistent risk-transfer flows | 1 |
| `donchian_breakout` | Price-channel breakout (Turtle) | New information pushes price out of its range; asymmetric exits | 1 |
| `xs_momentum` | Relative strength rotation / dual momentum | Under-reaction to sector news; performance-chasing flows | 4 |
| `rsi_pullback` | Short-term mean reversion in an uptrend | Transient selling pressure and over-reaction; liquidity provision | 1 |
| `xs_reversal` | Cross-sectional short-term reversal | Compensation for providing liquidity to forced sellers | 5 |
| `pairs_stat_arb` | Cointegration pairs, market neutral | Linked assets revert to equilibrium after idiosyncratic shocks | 2 |
| `low_vol` | Defensive / low-volatility anomaly | Leverage constraints make investors overpay for high-beta assets | 3 |
| `vol_managed` | Volatility timing | Volatility clusters; returns don't compensate for vol spikes | 1 |
| `turn_of_month` | Calendar / flow event | Predictable month-end inflows and window dressing | 1 |

Each class declares its thesis, rules, sizing, rebalance schedule, **invalidation conditions**, literature references and a parameter grid. Strategies that don't fit the universe are skipped with a stated reason (a single stock gets the five time-series strategies). The iteration step can wrap any strategy in a **market-regime filter** or a **portfolio volatility-target** overlay. Overlays count as extra trials for the multiple-testing haircut.

## How it avoids fooling itself

| Risk | Safeguard |
|---|---|
| Look-ahead bias | Signals use data up to the close of *t*. Default fills are at the **next open**, and the carried book earns the overnight gap. Every strategy is recomputed on truncated histories and must produce identical weights. |
| Unrealistic costs | Per-side commission, slippage and taxes (market presets, including India STT/stamp duty); **square-root market impact** from ADV and volatility; short borrow fees; leverage financing. Positions drift between rebalances, so no phantom turnover is charged. |
| Overfitting parameters | Rolling **walk-forward** re-selection (5y train / 1y test by default). Selection favours **plateaus** over isolated peaks. A parameter-sensitivity robustness score is reported. |
| Data snooping across many trials | **Deflated Sharpe ratio** (Bailey & López de Prado) using the total number of configurations tried, plus PSR, a bootstrap Sharpe CI and a t-stat. |
| Fragile execution | Stress tests at **2x and 3x costs**, a **one-session execution delay**, and an optimistic same-close fill for contrast. |
| Regime dependence | Performance split by bull/bear/sideways and low/normal/high volatility, calendar years, the benchmark's worst drawdowns, and named crises (GFC, COVID, 2022) on real data. |
| Single-asset dependence | P&L share of the top asset and a **leave-one-asset-out** Sharpe. |
| Liquidity | Max share of average daily volume traded, and a capacity estimate. |
| Survivorship / bad data | Data-quality table (gaps, stale quotes, >25% moves, late listings). Presets built from today's leaders carry an explicit survivorship warning. |

### Verdicts

Thresholds live in `Criteria` in [`config.py`](tradelab/config.py):

- **REJECTED** if any hard check fails:
  - walk-forward Sharpe < 0.5
  - does not beat the benchmark's Sharpe
  - max drawdown ≥ 30%
  - fewer than 55% of years positive
  - Sharpe at 2x costs below 50% of base
  - look-ahead detected
  - more than 5% of ADV traded
- **PROMISING** if all hard checks pass but the evidence is inconclusive (deflated Sharpe < 0.90 or parameter robustness < 0.5).
- **VALIDATED** if it passes everything.

Strategies are ranked by verdict, then by a 0-100 score. The final list takes **one variant per base strategy**, skips anything more than 80% correlated with a higher-ranked pick, and reports an equal-risk blend.

## Commands

```bash
python -m tradelab list                                     # strategies, presets, cost models
python -m tradelab scan --preset us-multi-asset             # regime + opportunity scan only
python -m tradelab backtest --preset us-sectors --strategy xs_momentum --param lookback=126 --param top_frac=0.33
python -m tradelab research --preset crypto --strategies trend_tsmom,vol_managed --no-iterate
python -m tradelab research --universe XOM,CVX,COP,EOG --benchmark XLE --start 2010-01-01 --rf 0.03
```

Useful flags:

| Flag | Meaning |
|---|---|
| `--source yahoo\|csv\|synthetic` | Where the data comes from |
| `--csv-dir` | Folder of `<TICKER>.csv` files with `Date, Open, High, Low, Close[, Adj Close], Volume` |
| `--costs us_etf\|us_equity\|us_small_cap\|india_equity\|crypto\|zero` | Cost-model preset |
| `--execution next_open\|next_close\|same_close` | When trades fill |
| `--capital` | Account size (drives market impact and the liquidity check) |
| `--rf` | Annual risk-free rate (cash earns it, and Sharpe is measured in excess of it) |
| `--max-grid` | Maximum parameter configurations per strategy |

Presets: `us-sectors`, `us-multi-asset`, `us-megacaps`, `india-reliance`, `india-leaders`, `crypto`.

### Python API

```python
from tradelab import config_from_preset
from tradelab.research import ResearchPipeline
from tradelab.report import write_report

cfg = config_from_preset("us-sectors", start="2005-01-01")
cfg.criteria.max_drawdown = 0.25          # tighten the bar if you like
res = ResearchPipeline(cfg).run()
print(res.ranking)
write_report(res)
```

### Adding a strategy

```python
# tradelab/strategies/my_idea.py
from .base import Strategy, hold_between_rebalances

class MyIdea(Strategy):
    name = "my_idea"
    family = "..."
    thesis = "Why the edge should exist (who is on the other side and why)."
    rules = "..."; sizing = "..."; rebalance = "..."
    invalidation = "What evidence would kill it."
    default_params = {"lookback": 60}
    param_grid = {"lookback": [20, 60, 120]}

    def generate_weights(self, data):
        # target weights decided with data up to each close, dates x assets
        ...
```

Register it in `STRATEGIES` in `tradelab/strategies/__init__.py`. The test suite then checks its weights, leverage and look-ahead behaviour automatically.

## Limitations (read before trusting any number)

- **Daily bars only.** Fills are modelled at the open or close; intraday dynamics and auction imbalances are not.
- **Price and volume only.** Fundamentals, options flow, news and earnings calendars are **not** used. The report lists them as unassessed. Macro context (VIX, 10y yield, dollar, oil, gold) is fetched when using Yahoo.
- **Yahoo Finance data** is free and convenient but not institutional grade. Check the data-quality table in each report, and use `--source csv` with vetted data for serious work.
- **Universe selection** is yours. Presets built from today's large caps suffer from survivorship bias, and the report flags this.
- **Costs** are approximations. Verify them against your broker and tax situation (taxes on gains are not modelled).
- A **VALIDATED** verdict means "survived these tests", not "will make money". Paper-trade first.

## Development

```bash
pip install -r requirements.txt
python -m pytest -q        # 57 tests: accounting, metrics, look-ahead, walk-forward, pipeline, report
```

Tests run offline on synthetic data. They cover:

- exact cost and fill accounting
- that every strategy (including overlays) is causal
- that a deliberately cheating strategy is caught
- that walk-forward selection only sees training data
- that the pairs code finds a planted cointegrated pair
- that nothing is VALIDATED on pure random walks
