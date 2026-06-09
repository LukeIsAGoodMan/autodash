"""Tests guard the load-bearing rule: rates are sum(num) / sum(den), never an
average of rates. These tests fail loudly if anyone reaches for .mean() on a
rate column.
"""
from __future__ import annotations

import polars as pl
import pytest

from src import metrics


def _toy() -> pl.DataFrame:
    # Two groups of two cells each. The averaging trap appears when responders
    # per cell vary widely while volume varies even more — averaging the rate
    # gives 0.15, summing-then-dividing gives 0.075.
    return pl.DataFrame({
        "campaign_month": ["2025-01", "2025-01", "2025-02", "2025-02"],
        "vs_band": ["A", "B", "A", "B"],
        "scorecard": [1, 1, 1, 1],
        "volume": [1000, 100, 800, 200],
        "responders": [50, 30, 40, 20],
        "Boards": [25, 15, 20, 10],
        "expected_responses": [60, 25, 50, 18],
        "expected_responses_xpm": [55, 28, 48, 22],
    })


def test_actual_response_rate_uses_sum_over_sum():
    df = _toy()
    out = metrics.aggregate_by(df, ["campaign_month"]).sort("campaign_month")
    # Jan: 80 / 1100 = 0.07272...; Feb: 60 / 1000 = 0.06
    assert out["actual_response_rate"][0] == pytest.approx(80 / 1100)
    assert out["actual_response_rate"][1] == pytest.approx(60 / 1000)
    # Verify that naively averaging would have given a different number.
    naive_jan = (50/1000 + 30/100) / 2     # 0.175
    assert out["actual_response_rate"][0] != pytest.approx(naive_jan)


def test_expected_rates_recomputed_from_sums():
    df = _toy()
    out = metrics.aggregate_by(df, ["campaign_month"]).sort("campaign_month")
    assert out["expected_rr_trm"][0] == pytest.approx((60 + 25) / 1100)
    assert out["expected_rr_xpm"][0] == pytest.approx((55 + 28) / 1100)


def test_actual_vs_expected_ratio():
    df = _toy()
    out = metrics.aggregate_by(df, ["campaign_month"]).sort("campaign_month")
    arr = 80 / 1100
    exp = 85 / 1100
    assert out["actual_vs_expected_trm"][0] == pytest.approx(arr / exp)


def test_kpi_totals_match_full_aggregate():
    df = _toy()
    k = metrics.kpi_totals(df)
    # Totals: volume=2100, responders=140, boards=70
    assert k["volume"] == 2100
    assert k["responders"] == 140
    assert k["Boards"] == 70
    assert k["actual_response_rate"] == pytest.approx(140 / 2100)


def test_safe_div_handles_zero_denominator():
    df = pl.DataFrame({
        "campaign_month": ["2025-01"],
        "volume": [0],
        "responders": [0],
        "Boards": [0],
        "expected_responses": [0],
        "expected_responses_xpm": [0],
    })
    out = metrics.aggregate_by(df, ["campaign_month"])
    assert out["actual_response_rate"][0] is None


def test_suppression_nulls_rates_but_keeps_counts():
    out = metrics.aggregate_by(_toy(), ["campaign_month", "vs_band"])
    masked = metrics.suppress_small_cells(out, threshold=500)
    # Row with vs_band='B', volume=100 < 500: rates should be null, counts kept.
    small = masked.filter(pl.col("vs_band") == "B").sort("campaign_month")
    assert small["volume"][0] == 100
    assert small["actual_response_rate"][0] is None
    assert small["actual_board_rate"][0] is None
