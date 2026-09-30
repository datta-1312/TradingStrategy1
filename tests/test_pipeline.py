from pathlib import Path

import pytest

from tradelab.config import ResearchConfig
from tradelab.report import write_report
from tradelab.research import ResearchPipeline


@pytest.fixture(scope="module")
def results():
    cfg = ResearchConfig(
        name="test", universe=["A", "B", "C", "D", "E", "F"], benchmark="BENCH", start="2010-01-01",
        end="2019-12-31", source="synthetic", cost_preset="us_etf", max_grid=4, wf_train_years=3,
        strategies=["trend_tsmom", "xs_momentum", "rsi_pullback", "pairs_stat_arb"], seed=5,
    )
    return ResearchPipeline(cfg, log=lambda *_: None).run()


def test_pipeline_produces_ranked_verdicts(results):
    assert len(results.ranking) >= 4
    assert set(results.ranking["status"]) <= {"VALIDATED", "PROMISING", "REJECTED"}
    assert results.n_trials == sum(len(v.runs) for v in results.validations)
    for v in results.validations:
        assert v.lookahead["passed"]
        assert v.walk_forward.returns.index[0] >= results.windows["wf"][0]["test_start"]
        assert 0 <= v.significance["dsr"] <= v.significance["psr"] + 1e-9
    # selected strategies are never rejected ones
    assert all(v.verdict.status != "REJECTED" for v in results.selected)


def test_report_has_all_sections(results, tmp_path: Path):
    path = write_report(results, str(tmp_path / "rep"))
    text = path.read_text()
    for heading in ["## 1. Executive Summary", "## 2. Market Analysis", "## 3. Strategy Details",
                    "## 4. Backtest Results", "## 5. Comparison & Ranking", "## 6. Final Recommendations",
                    "## 7. Deployment Considerations", "## 8. What Could Go Wrong", "Final check"]:
        assert heading in text
    assert "SYNTHETIC DATA" in text
    assert (path.parent / "charts" / "equity.png").exists()
    assert (path.parent / "results" / "ranking.csv").exists()


def test_single_asset_universe_runs(tmp_path):
    cfg = ResearchConfig(name="single", universe=["A"], benchmark="BENCH", start="2010-01-01", end="2019-12-31",
                         source="synthetic", max_grid=3, iterate=False, wf_train_years=3)
    res = ResearchPipeline(cfg, log=lambda *_: None).run()
    assert {n for n, _ in res.skipped} == {"xs_momentum", "xs_reversal", "pairs_stat_arb", "low_vol"}
    assert len(res.validations) == 5
    write_report(res, str(tmp_path / "single"))


def test_no_edge_in_pure_noise_is_not_validated():
    """On regime-free random walks nothing should pass as VALIDATED."""
    from tradelab.data import align_market_data, generate_synthetic
    tickers = ["A", "B", "C", "D", "E", "BENCH"]
    raw = generate_synthetic(tickers, "2008-01-01", "2019-12-31", seed=21, benchmark="BENCH", regime_switching=False)
    data = align_market_data(raw, tickers[:-1], "BENCH", "2008-01-01", "2019-12-31", source="synthetic")
    cfg = ResearchConfig(name="noise", universe=tickers[:-1], benchmark="BENCH", start="2008-01-01",
                         source="synthetic", max_grid=6, iterate=False)
    res = ResearchPipeline(cfg, data=data, log=lambda *_: None).run()
    assert "VALIDATED" not in set(res.ranking["status"])
