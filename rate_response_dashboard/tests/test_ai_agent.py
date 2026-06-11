"""Tests for the deterministic Stage-1 AI report pipeline.

Marts are constructed in-memory so the tests do not depend on parquet on
disk. Each unit confines itself to one stage (snapshot_builder, mom_yoy,
model_compare) and asserts the boundary contract, not implementation
detail.
"""
from __future__ import annotations

import polars as pl
import pytest

from src.ai_agent import mom_yoy, model_compare, snapshot_builder
from src.ai_agent.facts import MaturityInfo
from src.ai_agent.report.renderer import render_html
from src.ai_agent.orchestrator import build_report_package  # noqa: F401  -- import-checked, not invoked here


def _rollup_df() -> pl.DataFrame:
    """Three months × two products × two vs_bands. Hand-tuned so each test
    assertion is computable on paper."""
    rows = []
    for cm in ["2025-06", "2026-05", "2026-06"]:
        for af in ["$75/$99", "$0/$0"]:
            for vb in ["550-600", "601-639"]:
                vol = {"$75/$99": 10_000, "$0/$0": 5_000}[af]
                # Drift NRR up over months so MoM/YoY directions are predictable.
                base = {"$75/$99": 0.010, "$0/$0": 0.007}[af] + {"2025-06": 0, "2026-05": 0.0005, "2026-06": 0.001}[cm]
                resp = int(vol * base)
                rows.append({
                    "campaign_month": cm,
                    "annual_fee": af,
                    "vs_band": vb,
                    "scorecard": 12,
                    "Prospect_type": "Prospecting",
                    "rm_flag": "N",
                    "volume": float(vol),
                    "responders": float(resp),
                    "Boards": float(int(resp * 0.85)),
                    "expected_responses": float(resp * 1.02),
                    "expected_responses_xpm": float(resp * 0.98),
                })
    return pl.DataFrame(rows)


def _maturity_full() -> dict[str, MaturityInfo]:
    return {
        "2025-06": MaturityInfo("2025-06", "full", True),
        "2026-05": MaturityInfo("2026-05", "full", True),
        "2026-06": MaturityInfo("2026-06", "partial", False),  # latest is preliminary
    }


def _cfg() -> dict:
    return {
        "dashboard": {"small_cell_threshold": 100, "title": "Rate Response Dashboard"},
        "mart": {"maturity_threshold_months": 3},
        "paths": {"logs_dir": "./data/logs"},
    }


# ============================================================================
# snapshot_builder
# ============================================================================


def test_snapshot_picks_latest_and_populates_trends():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    assert facts.latest_month == "2026-06"
    assert facts.months_in_scope == ["2025-06", "2026-05", "2026-06"]
    assert len(facts.overall_trend) == 3
    # 3 months × 2 products = 6 product_trend rows
    assert len(facts.product_trend) == 6


def test_snapshot_segment_latest_and_prior_present():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    assert set(facts.segment_latest.keys()) >= {"vs_band", "scorecard", "Prospect_type"}
    # Latest (2026-06) and prior (2026-05) both have data for vs_band.
    assert facts.segment_latest["vs_band"], "expected latest vs_band rows"
    assert facts.segment_prior["vs_band"], "expected prior vs_band rows"


def test_snapshot_empty_mart_returns_empty_facts():
    empty = pl.DataFrame()
    facts = snapshot_builder.build(empty, {}, _cfg())
    assert facts.latest_month == ""
    assert facts.months_in_scope == []
    assert facts.overall_trend == []


# ============================================================================
# mom_yoy
# ============================================================================


def test_mom_yoy_finds_correct_anchors():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts)
    # Latest=2026-06, prior=2026-05, YoY anchor=2025-06 — all present in fixture.
    assert mm.latest_month == "2026-06"
    assert len(mm.overall_mom) == 4  # 4 metrics
    assert len(mm.overall_yoy) == 4
    nrr_mom = next(m for m in mm.overall_mom if m.metric == "nrr")
    assert nrr_mom.current_month == "2026-06"
    assert nrr_mom.prior_month == "2026-05"
    # NRR was +0.0005 from 2026-05 to 2026-06 → +5 bps; direction "up".
    assert nrr_mom.delta_bps == pytest.approx(5.0, abs=0.5)
    assert nrr_mom.direction == "up"


def test_mom_yoy_handles_missing_anchors():
    """Only one month → no MoM, no YoY. Should not raise."""
    df = _rollup_df().filter(pl.col("campaign_month") == "2026-06")
    facts = snapshot_builder.build(df, _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts)
    assert mm.overall_mom == []
    assert mm.overall_yoy == []
    assert mm.biggest_movers == []


def test_biggest_movers_sorted_by_absolute_delta():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts, top_k=10)
    if mm.biggest_movers:
        deltas = [abs(m.delta_bps or 0) for m in mm.biggest_movers]
        assert deltas == sorted(deltas, reverse=True)


# ============================================================================
# model_compare
# ============================================================================


def _decile_port_df() -> pl.DataFrame:
    """Two months. Deciles 1..20 with monotone-decreasing response rate so
    KS > 0 and misrank_count == 0."""
    rows = []
    for cm in ["2026-05", "2026-06"]:
        for d in range(1, 21):
            vol = 50_000.0
            rr = 0.025 * (1.0 - (d - 1) / 19) ** 1.3 + 0.003
            resp = vol * rr
            rows.append({
                "campaign_month": cm,
                "decile": d,
                "volume": vol,
                "responders": resp,
                "Boards": resp * 0.85,
            })
    return pl.DataFrame(rows)


def test_model_compare_trm_score_available_xpm_marked_unavailable():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    comp = model_compare.compute(_rollup_df(), _decile_port_df(), facts)
    trm = next(s for s in comp.headline if s.model == "TRM")
    xpm = next(s for s in comp.headline if s.model == "XPM")
    assert trm.available is True
    assert trm.ks is not None and trm.ks > 0
    assert trm.auc is not None and 0.5 < trm.auc < 1.0
    assert trm.misrank_count == 0
    assert xpm.available is False
    assert "XPM07" in (xpm.note or "")


def test_calibration_trend_respects_has_xpm():
    """When has_xpm=False, the XPM calibration point must be marked
    unavailable — surfacing a misleading 0/null A/E would be worse than
    saying nothing."""
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    comp = model_compare.compute(_rollup_df(), _decile_port_df(), facts)
    xpm_latest = next(
        c for c in comp.calibration_trend
        if c.campaign_month == "2026-06" and c.model == "XPM"
    )
    assert xpm_latest.available is False
    trm_latest = next(
        c for c in comp.calibration_trend
        if c.campaign_month == "2026-06" and c.model == "TRM"
    )
    assert trm_latest.available is True


# ============================================================================
# renderer
# ============================================================================


def test_renderer_produces_non_empty_html():
    from src.ai_agent.facts import ReportPackage
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts)
    comp = model_compare.compute(_rollup_df(), _decile_port_df(), facts)
    pkg = ReportPackage(facts=facts, mom_yoy=mm, model=comp,
                        config_snapshot={"report_title": "Test Title",
                                         "small_cell_threshold": 100,
                                         "maturity_threshold_months": 3})
    html = render_html(pkg)
    assert "<html" in html.lower()
    assert "Test Title" in html
    # Latest month must show somewhere in the document.
    assert "2026-06" in html
    # Watermark must be present so reports can never be mistaken for final.
    assert "AI-generated draft" in html
