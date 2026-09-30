"""Static report charts (matplotlib, PNG).

Style: one y-axis per chart, thin 2px lines, solid hairline gridlines, recessive
axes, categorical colours in a fixed validated order (never by rank), a neutral
gray for the benchmark, and a legend whenever there are two or more series.
Every chart has a table twin in the Markdown report.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap, TwoSlopeNorm  # noqa: E402

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
BENCH = MUTED
DIVERGING = LinearSegmentedColormap.from_list("div", ["#e34948", "#f0efec", "#2a78d6"])

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "font.family": "sans-serif", "font.size": 9.5,
    "axes.edgecolor": BASELINE, "axes.linewidth": 0.8, "axes.labelcolor": INK_2,
    "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlecolor": INK, "axes.titlelocation": "left",
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8, "grid.linestyle": "-",
    "xtick.color": MUTED, "ytick.color": MUTED, "xtick.labelcolor": INK_2, "ytick.labelcolor": INK_2,
    "legend.frameon": False, "legend.fontsize": 8.5, "lines.linewidth": 2.0,
    "lines.solid_capstyle": "round", "lines.solid_joinstyle": "round",
})


def colour_map(names: list[str]) -> dict[str, str]:
    """Stable entity -> colour assignment (by the order strategies were ranked once)."""
    return {n: SERIES[i % len(SERIES)] for i, n in enumerate(names)}


def _save(fig, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=130, bbox_inches="tight")
    plt.close(fig)
    return path.name


def equity_chart(series: dict[str, pd.Series], bench: pd.Series, bench_name: str, colours: dict,
                 path: Path, title: str) -> str:
    fig, ax = plt.subplots(figsize=(9, 4.2))
    b = (1 + bench).cumprod()
    ax.plot(b.index, b, color=BENCH, lw=1.6, label=f"{bench_name} (benchmark)")
    for name, r in series.items():
        eq = (1 + r).cumprod()
        ax.plot(eq.index, eq, color=colours[name], label=name)
    ax.set_yscale("log")
    ax.yaxis.set_major_locator(matplotlib.ticker.LogLocator(base=10, subs=(1.0, 1.5, 2.0, 3.0, 5.0, 7.0)))
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.1f}x"))
    ax.yaxis.set_minor_formatter(matplotlib.ticker.NullFormatter())
    ax.set_title(title)
    ax.set_ylabel("Growth of 1 (log scale)")
    ax.legend(loc="upper left", ncol=2)
    return _save(fig, path)


def drawdown_chart(series: dict[str, pd.Series], bench: pd.Series, bench_name: str, colours: dict,
                   path: Path, title: str) -> str:
    fig, ax = plt.subplots(figsize=(9, 3.2))

    def dd(r):
        eq = (1 + r).cumprod()
        return eq / eq.cummax() - 1

    bd = dd(bench)
    ax.fill_between(bd.index, bd, 0, color=BENCH, alpha=0.12, lw=0)
    ax.plot(bd.index, bd, color=BENCH, lw=1.4, label=f"{bench_name} (benchmark)")
    for name, r in series.items():
        ax.plot(r.index, dd(r), color=colours[name], lw=1.6, label=name)
    ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    ax.set_title(title)
    ax.legend(loc="lower left", ncol=2)
    return _save(fig, path)


def sharpe_bars(ranking: pd.DataFrame, bench_sharpe: float, min_sharpe: float, path: Path) -> str:
    df = ranking.sort_values("wf_sharpe")
    h = max(2.5, 0.28 * len(df) + 1)
    fig, ax = plt.subplots(figsize=(8, h))
    y = np.arange(len(df))
    ax.barh(y, df["wf_sharpe"], height=0.55, color=SERIES[0], lw=0)
    ax.set_yticks(y, [f"{s}  [{st}]" for s, st in zip(df["strategy"], df["status"])])
    ax.axvline(0, color=BASELINE, lw=1)
    ax.axvline(bench_sharpe, color=INK_2, lw=1.2)
    ax.axvline(min_sharpe, color=MUTED, lw=1.2)
    left, right = (min_sharpe, bench_sharpe) if min_sharpe <= bench_sharpe else (bench_sharpe, min_sharpe)
    ax.text(left, len(df) - 0.45, "minimum bar " if left == min_sharpe else "benchmark ", color=INK_2,
            fontsize=8, va="bottom", ha="right")
    ax.text(right, len(df) - 0.45, " benchmark" if right == bench_sharpe else " minimum bar", color=INK_2,
            fontsize=8, va="bottom", ha="left")
    ax.grid(axis="y", visible=False)
    ax.set_title("Walk-forward (out-of-sample) Sharpe ratio by strategy")
    ax.set_xlabel("Sharpe ratio")
    return _save(fig, path)


def sensitivity_strip(sens: dict[str, pd.DataFrame], chosen: dict[str, float], path: Path) -> str:
    names = list(sens)
    fig, ax = plt.subplots(figsize=(9, max(2.5, 0.32 * len(names) + 1)))
    rng = np.random.default_rng(0)
    for i, n in enumerate(names):
        s = sens[n]["full_sharpe"].to_numpy()
        ax.scatter(s, i + rng.uniform(-0.12, 0.12, len(s)), s=22, color=SERIES[0], alpha=0.55,
                   edgecolors=SURFACE, linewidths=1.0, zorder=3)
        ax.scatter([np.median(s)], [i], marker="|", s=260, color=INK, zorder=4)
    ax.set_yticks(range(len(names)), names)
    ax.invert_yaxis()  # best-ranked strategy on top
    ax.axvline(0, color=BASELINE, lw=1)
    ax.grid(axis="y", visible=False)
    ax.set_title("Parameter sensitivity: full-period Sharpe of every grid configuration")
    ax.set_xlabel("Sharpe ratio (dots = configurations, bar = median)")
    return _save(fig, path)


def heatmap(df: pd.DataFrame, path: Path, title: str, fmt_pct: bool = True, vmax: Optional[float] = None) -> str:
    vals = df.to_numpy(dtype=float)
    lim = vmax or (np.nanmax(np.abs(vals)) if np.isfinite(vals).any() else 1.0) or 1.0
    fig, ax = plt.subplots(figsize=(max(5, 0.55 * df.shape[1] + 3), max(2.2, 0.38 * df.shape[0] + 1.2)))
    im = ax.imshow(vals, cmap=DIVERGING, norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
    ax.set_xticks(range(df.shape[1]), [str(c) for c in df.columns], rotation=45 if df.shape[1] > 8 else 0,
                  ha="right" if df.shape[1] > 8 else "center")
    ax.set_yticks(range(df.shape[0]), df.index)
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.outline.set_visible(False)
    if fmt_pct:
        cb.ax.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=0))
    cb.ax.tick_params(colors=MUTED, labelcolor=INK_2)
    ax.set_title(title)
    return _save(fig, path)


def regime_chart(bench: pd.Series, regimes: pd.DataFrame, bench_name: str, path: Path, years: int = 5) -> str:
    b = bench.iloc[-252 * years:]
    reg = regimes["trend"].reindex(b.index)
    sma = bench.rolling(200).mean().reindex(b.index)
    fig, ax = plt.subplots(figsize=(9, 3.4))
    bear = (reg == "bear").to_numpy()
    lo, hi = float(b.min()) * 0.97, float(b.max()) * 1.03
    ax.fill_between(b.index, lo, hi, where=bear, color=MUTED, alpha=0.14, lw=0, label="bear regime")
    ax.plot(b.index, b, color=SERIES[0], label=bench_name)
    ax.plot(sma.index, sma, color=INK_2, lw=1.2, label="200-day average")
    ax.set_ylim(lo, hi)
    ax.set_title(f"{bench_name}: price, 200-day average and bear-regime periods (last {years} years)")
    ax.legend(loc="upper left", ncol=3)
    return _save(fig, path)
