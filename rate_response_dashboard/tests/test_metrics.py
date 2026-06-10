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


def _toy_decile(responders: list[int] | None = None) -> pl.DataFrame:
    # One scorecard, 10 deciles. Decile 1 = highest score = highest response.
    return pl.DataFrame({
        "campaign_month": ["2026-05"] * 10,
        "scorecard": [1] * 10,
        "decile": list(range(1, 11)),
        "volume":     [1000] * 10,
        "responders": responders or [200, 150, 120, 100, 80, 60, 50, 40, 30, 20],
        "Boards":     [100,  80,  60,  50, 40, 30, 25, 20, 15, 10],
    })


def test_decile_summary_cum_capture_reaches_one():
    s = metrics.decile_summary(_toy_decile(), scorecard=1)
    assert s["cum_capture"][-1] == pytest.approx(1.0)
    assert s["cum_volume_pct"][-1] == pytest.approx(1.0)


def test_ks_value_matches_per_decile_max():
    s = metrics.decile_summary(_toy_decile(), scorecard=1)
    assert metrics.ks_value(_toy_decile(), scorecard=1) == pytest.approx(float(s["per_decile_ks"].max()))


def test_ks_returns_none_when_no_responders():
    df = _toy_decile().with_columns(responders=pl.lit(0, dtype=pl.Int64))
    assert metrics.ks_value(df, scorecard=1) is None


def test_decile_summary_lift_top_decile_above_one():
    s = metrics.decile_summary(_toy_decile(), scorecard=1)
    assert s["lift"][0] > 1.0
    assert s["lift"][-1] < 1.0


def test_misrank_zero_on_well_ordered():
    # Strictly decreasing responders → response_rate strictly decreasing
    # (since volume is constant) → no misrank.
    s = metrics.decile_summary(_toy_decile(), scorecard=1)
    assert s["misrank"].sum() == 0


def test_misrank_flags_violations():
    # Swap deciles 3 and 4 so D4 has higher responders than D3.
    bad = [200, 150, 100, 120, 80, 60, 50, 40, 30, 20]
    s = metrics.decile_summary(_toy_decile(responders=bad), scorecard=1)
    # D4 (0-indexed row 3) should be flagged because its rate > D3's rate.
    assert s["misrank"][3] == 1
    assert s["misrank"].sum() >= 1


def test_auc_perfect_rank_above_random():
    # Our toy data is strongly rank-ordered; AUC should be well above 0.5.
    auc = metrics.auc_value(_toy_decile(), scorecard=1)
    assert auc is not None and auc > 0.7


def test_auc_random_near_half():
    # Flat distribution of responders → no signal → AUC ≈ 0.5.
    flat = [85] * 10
    auc = metrics.auc_value(_toy_decile(responders=flat), scorecard=1)
    assert auc == pytest.approx(0.5, abs=0.01)


def test_gini_equals_two_auc_minus_one():
    auc = metrics.auc_value(_toy_decile(), scorecard=1)
    gini = metrics.gini_value(_toy_decile(), scorecard=1)
    assert gini == pytest.approx(2 * auc - 1)


def test_per_decile_ks_max_equals_ks_value():
    s = metrics.decile_summary(_toy_decile(), scorecard=1)
    assert s["per_decile_ks"].max() == pytest.approx(metrics.ks_value(_toy_decile(), scorecard=1))


def test_suppression_nulls_rates_but_keeps_counts():
    out = metrics.aggregate_by(_toy(), ["campaign_month", "vs_band"])
    masked = metrics.suppress_small_cells(out, threshold=500)
    # Row with vs_band='B', volume=100 < 500: rates should be null, counts kept.
    small = masked.filter(pl.col("vs_band") == "B").sort("campaign_month")
    assert small["volume"][0] == 100
    assert small["actual_response_rate"][0] is None
    assert small["actual_board_rate"][0] is None
