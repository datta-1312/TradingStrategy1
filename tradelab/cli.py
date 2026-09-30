"""Command-line interface.

Examples::

    python -m tradelab research --preset us-sectors
    python -m tradelab research --universe AAPL,MSFT,NVDA --benchmark SPY --costs us_equity
    python -m tradelab research --preset india-reliance --source csv --csv-dir data/
    python -m tradelab research --preset us-sectors --source synthetic      # offline demo
    python -m tradelab scan --preset us-multi-asset
    python -m tradelab backtest --preset us-sectors --strategy trend_tsmom --param lookback=126
    python -m tradelab list
"""
from __future__ import annotations

import argparse
import ast
import sys

import pandas as pd

from .config import PRESETS, ResearchConfig, config_from_preset
from .costs import COST_PRESETS


def _config(args) -> ResearchConfig:
    overrides = dict(
        start=args.start, end=args.end, source=args.source, csv_dir=args.csv_dir, cost_preset=args.costs,
        capital=args.capital, execution=args.execution, output_dir=args.out, risk_free_rate=args.rf,
        seed=args.seed,
    )
    if getattr(args, "strategies", None):
        overrides["strategies"] = args.strategies.split(",")
    if getattr(args, "no_iterate", False):
        overrides["iterate"] = False
    if getattr(args, "max_grid", None):
        overrides["max_grid"] = args.max_grid
    if args.universe:
        if not args.benchmark:
            sys.exit("--universe requires --benchmark")
        name = args.name or "custom"
        base = dict(PRESETS[args.preset]) if args.preset else {}
        base.update(universe=args.universe.split(","), benchmark=args.benchmark)
        base.update({k: v for k, v in overrides.items() if v is not None})
        base.setdefault("notes", [])
        return ResearchConfig(name=name, **base)
    if not args.preset:
        sys.exit("Give --preset or --universe/--benchmark")
    cfg = config_from_preset(args.preset, **overrides)
    if args.benchmark:
        cfg.benchmark = args.benchmark
    if args.name:
        cfg.name = args.name
    return cfg


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--preset", choices=sorted(PRESETS), help="named universe preset")
    p.add_argument("--universe", help="comma-separated tickers (overrides preset universe)")
    p.add_argument("--benchmark", help="benchmark ticker, e.g. SPY or ^NSEI")
    p.add_argument("--name", help="run name used for the report folder")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--source", choices=["yahoo", "csv", "synthetic"])
    p.add_argument("--csv-dir")
    p.add_argument("--costs", choices=sorted(COST_PRESETS), help="transaction-cost preset")
    p.add_argument("--capital", type=float)
    p.add_argument("--execution", choices=["next_open", "next_close", "same_close"])
    p.add_argument("--rf", type=float, help="annual risk-free rate, e.g. 0.04")
    p.add_argument("--out", help="output directory for reports")
    p.add_argument("--seed", type=int)


def _parse_param(s: str):
    k, _, v = s.partition("=")
    try:
        return k, ast.literal_eval(v)
    except (ValueError, SyntaxError):
        return k, v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="tradelab", description="Systematic trading-strategy research pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("research", help="run the full 8-step research pipeline and write a report")
    _common(r)
    r.add_argument("--strategies", help="comma-separated subset of strategies")
    r.add_argument("--max-grid", type=int, help="max parameter combinations per strategy")
    r.add_argument("--no-iterate", action="store_true", help="skip the overlay iteration round")

    s = sub.add_parser("scan", help="market overview and opportunity scan only")
    _common(s)

    b = sub.add_parser("backtest", help="backtest one strategy with fixed parameters")
    _common(b)
    b.add_argument("--strategy", required=True)
    b.add_argument("--param", action="append", default=[], help="key=value (repeatable)")

    sub.add_parser("list", help="list strategies, presets and cost models")
    args = ap.parse_args(argv)

    pd.set_option("display.width", 200)
    pd.set_option("display.max_columns", 30)

    if args.cmd == "list":
        from .strategies import STRATEGIES
        print("Strategies:")
        for n, cls in STRATEGIES.items():
            print(f"  {n:<20} {cls.family}  (min assets {cls.min_assets})")
        print("\nPresets:")
        for n, p in PRESETS.items():
            print(f"  {n:<16} {p['benchmark']:<8} {', '.join(p['universe'])}")
        print("\nCost models:")
        for c in COST_PRESETS.values():
            print(f"  {c.describe()}")
        return 0

    cfg = _config(args)

    if args.cmd == "research":
        from .report import write_report
        from .research import ResearchPipeline
        res = ResearchPipeline(cfg).run()
        path = write_report(res)
        print("\n" + res.ranking[["rank", "strategy", "status", "score", "wf_sharpe", "wf_cagr", "wf_max_dd",
                                  "dsr"]].to_string(index=False))
        print(f"\nSelected: {', '.join(v.name for v in res.selected) or 'none'}")
        print(f"Report: {path}")
        return 0

    from .data import load_market_data
    data = load_market_data(cfg.universe, cfg.benchmark, cfg.start, cfg.end, cfg.source, cfg.csv_dir,
                            cfg.cache_dir, seed=cfg.seed)

    if args.cmd == "scan":
        from .market import market_overview, opportunity_scan
        ov = market_overview(data, cfg.periods_per_year)
        print("FACTS")
        for k, v in ov["facts"].items():
            print(f"  {k:<26} {v:.4f}" if isinstance(v, float) else f"  {k:<26} {v}")
        print("\nINTERPRETATION (heuristic)")
        for line in ov["interpretation"]:
            print(f"  - {line}")
        print("\nOPPORTUNITY SCAN")
        print(opportunity_scan(data, cfg.periods_per_year).round(3).to_string())
        return 0

    if args.cmd == "backtest":
        from .backtest import run_backtest
        from .costs import get_cost_model
        from .metrics import performance_summary
        from .strategies import get_strategy
        strat = get_strategy(args.strategy)(**dict(_parse_param(p) for p in args.param))
        res = run_backtest(strat.generate_weights(data), data, get_cost_model(cfg.cost_preset), cfg.execution,
                           cfg.capital, cfg.risk_free_rate, cfg.periods_per_year, label=strat.label)
        m = performance_summary(res.returns.iloc[260:], data.benchmark_returns().iloc[260:], cfg.periods_per_year,
                                cfg.risk_free_rate, res.trades, res.turnover, res.weights, res.cost)
        print(strat.label, "(full sample, in-sample by construction; use `research` for validation)")
        for k, v in m.items():
            print(f"  {k:<24} {v:.4f}" if isinstance(v, float) else f"  {k:<24} {v}")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
