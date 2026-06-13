"""Tests for the deterministic Stage-1 AI report pipeline.

Marts are constructed in-memory so the tests do not depend on parquet on
disk. Each unit confines itself to one stage (snapshot_builder, mom_yoy,
mix, big_mac, model_catch, model_compare) and asserts the boundary
contract, not implementation detail.
"""
from __future__ import annotations

import polars as pl
import pytest

from src.ai_agent import (
    big_mac,
    combinations,
    mix_analysis,
    model_catch,
    model_compare,
    mom_yoy,
    slice_trends,
    snapshot_builder,
)
from src.ai_agent.facts import MaturityInfo
from src.ai_agent.report.renderer import render_html


# ============================================================================
# Fixtures
# ============================================================================


def _rollup_df() -> pl.DataFrame:
    """Three months × two products × two vs_bands × rm_flag {0,1} × tm {0,1}.
    Tier and Prospect_type are seeded so the Big Mac filter (Prospecting +
    rm_flag=0 + tm=0 + tier in {1,21}) matches some rows. NRR drifts up
    over months so MoM/YoY direction tests are predictable."""
    rows = []
    for cm in ["2025-06", "2026-05", "2026-06"]:
        for af in ["$75/$99", "$0/$0"]:
            for vb in ["550-600", "601-639"]:
                for rm in [0, 1]:
                    for tm in [0, 1]:
                        for prospect in ["Prospecting", "Closed Remarket"]:
                            for tier in [1, 5, 21, 30]:
                                vol = {"$75/$99": 5_000, "$0/$0": 2_500}[af]
                                base = ({"$75/$99": 0.010, "$0/$0": 0.007}[af]
                                        + {"2025-06": 0, "2026-05": 0.0005, "2026-06": 0.001}[cm])
                                resp = int(vol * base)
                                # Calibrate expected slightly off actual so model_catch
                                # produces a mix of verdicts.
                                exp_factor = 1.02 if tier <= 5 else 0.95
                                rows.append({
                                    "campaign_month": cm,
                                    "annual_fee": af,
                                    "vs_band": vb,
                                    "scorecard": 12,
                                    "Prospect_type": prospect,
                                    "rm_flag": rm,
                                    "trm10_tier": tier,
                                    "times_mailed_12mo_cnt": tm,
                                    "volume": float(vol),
                                    "responders": float(resp),
                                    "Boards": float(int(resp * 0.85)),
                                    "expected_responses": float(resp * exp_factor),
                                    "expected_responses_xpm": float(resp * 0.98),
                                })
    return pl.DataFrame(rows)


def _maturity_full() -> dict[str, MaturityInfo]:
    return {
        "2025-06": MaturityInfo("2025-06", "full", True),
        "2026-05": MaturityInfo("2026-05", "full", True),
        "2026-06": MaturityInfo("2026-06", "partial", False),
    }


def _cfg() -> dict:
    return {
        "dashboard": {"small_cell_threshold": 100, "title": "Rate Response Dashboard"},
        "mart": {"maturity_threshold_months": 3},
        "paths": {"logs_dir": "./data/logs"},
        "ai_agent": {
            "chart_lookback_months": 15,
            "big_mac": {
                "Prospect_type": "Prospecting",
                "rm_flag": 0,
                "times_mailed_12mo_cnt": 0,
                "trm10_tier_in": [1, 21],
                "drill_dim": "vs_band",
            },
            "slice_dims": ["annual_fee", "vs_band", "rm_flag"],
            "combinations": {
                "dim_pairs": [
                    ["annual_fee", "vs_band"],
                    ["annual_fee", "Prospect_type"],
                ],
                "min_combo_volume": 1000,   # low to fit small fixture
                "top_k": 5,
            },
        },
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


def test_snapshot_segment_trend_covers_all_months():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    # The cfg specifies 3 slice dims; all must appear in segment_trend.
    assert set(facts.segment_trend.keys()) == {"annual_fee", "vs_band", "rm_flag"}
    # vs_band has 2 values × 3 months = 6 rows
    vs_band_rows = facts.segment_trend["vs_band"]
    months = {r.campaign_month for r in vs_band_rows}
    assert months == {"2025-06", "2026-05", "2026-06"}


def test_snapshot_big_mac_cohort_populated():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    # Fixture has Prospecting + rm_flag=0 + tm=0 + tier {1, 21} rows.
    assert not _is_empty_trend(facts.big_mac_trend)
    assert facts.big_mac_drill_dim == "vs_band"
    bm_months = {r.campaign_month for r in facts.big_mac_trend}
    assert bm_months == {"2025-06", "2026-05", "2026-06"}


def test_snapshot_empty_mart_returns_empty_facts():
    empty = pl.DataFrame()
    facts = snapshot_builder.build(empty, {}, _cfg())
    assert facts.latest_month == ""
    assert facts.months_in_scope == []
    assert facts.overall_trend == []
    assert facts.segment_trend == {}


def test_big_mac_filter_expr_handles_in_keys():
    expr = snapshot_builder.big_mac_filter_expr({
        "Prospect_type": "Prospecting",
        "trm10_tier_in": [1, 21],
        "drill_dim": "vs_band",   # must be ignored, not turned into a filter
    })
    assert expr is not None
    # Apply to a tiny frame and check it filters as expected.
    df = pl.DataFrame({
        "Prospect_type": ["Prospecting", "Prospecting", "Closed Remarket"],
        "trm10_tier": [1, 5, 1],
        "campaign_month": ["x", "x", "x"],
    })
    out = df.filter(expr)
    # Only the first row (Prospecting + tier 1) survives.
    assert out.height == 1


# ============================================================================
# mom_yoy
# ============================================================================


def test_mom_yoy_finds_correct_anchors_and_signs():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts)
    assert mm.latest_month == "2026-06"
    assert len(mm.overall_mom) == 4
    nrr_mom = next(m for m in mm.overall_mom if m.metric == "nrr")
    assert nrr_mom.prior_month == "2026-05"
    # Direction must be up (base NRR drifts +0.0005 per month for both products);
    # exact magnitude depends on product-mix weighting + integer rounding in the
    # fixture, so we assert sign and a wide-tolerance range.
    assert nrr_mom.direction == "up"
    assert 2.0 < nrr_mom.delta_bps < 15.0


def test_mom_yoy_biggest_movers_sorted_and_non_null():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts, top_k=10)
    if not mm.biggest_movers:
        pytest.skip("fixture too small to surface movers")
    deltas = [abs(m.delta_bps or 0) for m in mm.biggest_movers]
    assert deltas == sorted(deltas, reverse=True)
    # All entries should have non-null current and prior NRR.
    assert all(m.current_value is not None and m.prior_value is not None
               for m in mm.biggest_movers)


# ============================================================================
# mix_analysis
# ============================================================================


def test_mix_analysis_shares_sum_to_one_per_dim_per_month():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mix = mix_analysis.compute(facts)
    for dim, shifts in mix.by_dim.items():
        # Latest-month shares should sum to ≈ 1.0.
        latest_share_total = sum(s.current_share or 0.0 for s in shifts)
        assert latest_share_total == pytest.approx(1.0, abs=1e-6), (
            f"{dim} latest shares sum to {latest_share_total}, expected ~1.0"
        )


def test_mix_analysis_top_shifts_ranked_by_magnitude():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mix = mix_analysis.compute(facts, top_k=5)
    if not mix.top_shifts:
        pytest.skip("fixture has uniform mix")
    deltas = [abs(s.delta_share_pp or 0) for s in mix.top_shifts]
    assert deltas == sorted(deltas, reverse=True)


# ============================================================================
# big_mac
# ============================================================================


def test_big_mac_drilldown_picks_drop_and_gain():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    bm = big_mac.compute(facts, _cfg()["ai_agent"]["big_mac"])
    assert bm.cohort_empty is False
    assert bm.drill_dim == "vs_band"
    # In our fixture NRR is monotone-up across months, so biggest_drop may
    # be None while biggest_gain should be non-None.
    if bm.biggest_drop is not None:
        assert (bm.biggest_drop.delta_bps or 0) <= 0
    if bm.biggest_gain is not None:
        assert (bm.biggest_gain.delta_bps or 0) >= 0


def test_big_mac_empty_when_filter_unsatisfied():
    df = _rollup_df().filter(pl.col("Prospect_type") != "Prospecting")
    facts = snapshot_builder.build(df, _maturity_full(), _cfg())
    bm = big_mac.compute(facts, _cfg()["ai_agent"]["big_mac"])
    assert bm.cohort_empty is True


# ============================================================================
# combinations
# ============================================================================


def test_combinations_returns_gainers_and_losers_with_volume_filter():
    combos = combinations.compute(_rollup_df(), _cfg())
    assert combos.latest_month == "2026-06"
    assert combos.prior_month == "2026-05"
    assert combos.min_volume == 1000
    # NRR drifts up across months → expect at least one gainer or all flat.
    # Losers may be empty in this fixture; only the structure is asserted.
    for m in combos.top_gainers + combos.top_losers:
        # Volume floor must have been respected on the current month.
        assert m.current_volume >= 1000
        # Direction matches sign of delta_bps.
        if m.delta_bps > 1:
            assert m.direction == "up"
        elif m.delta_bps < -1:
            assert m.direction == "down"


def test_combinations_empty_when_single_month():
    df = _rollup_df().filter(pl.col("campaign_month") == "2026-06")
    combos = combinations.compute(df, _cfg())
    assert combos.prior_month is None
    assert combos.top_gainers == []
    assert combos.top_losers == []


# ============================================================================
# model_catch
# ============================================================================


def test_model_catch_summary_buckets_each_row():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts)
    mc = model_catch.compute(facts, mm)
    if not mc.rows:
        pytest.skip("no material movers in fixture")
    # Sum of bucket counts equals number of rows analysed.
    assert sum(mc.trm_summary.values()) == len(mc.rows)
    assert sum(mc.xpm_summary.values()) == len(mc.rows)
    for r in mc.rows:
        assert r.trm_verdict in {"match", "partial", "miss", "n/a"}
        assert r.xpm_verdict in {"match", "partial", "miss", "n/a"}


# ============================================================================
# model_compare
# ============================================================================


def _decile_port_df() -> pl.DataFrame:
    rows = []
    for cm in ["2026-05", "2026-06"]:
        for d in range(1, 21):
            vol = 50_000.0
            rr = 0.025 * (1.0 - (d - 1) / 19) ** 1.3 + 0.003
            resp = vol * rr
            rows.append({
                "campaign_month": cm, "decile": d,
                "volume": vol, "responders": resp, "Boards": resp * 0.85,
            })
    return pl.DataFrame(rows)


def test_model_compare_trm_available_xpm_unavailable():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    comp = model_compare.compute(_rollup_df(), _decile_port_df(), facts)
    trm = next(s for s in comp.headline if s.model == "TRM")
    xpm = next(s for s in comp.headline if s.model == "XPM")
    assert trm.available is True
    assert trm.ks is not None and trm.ks > 0
    assert xpm.available is False


def test_calibration_trend_respects_has_xpm():
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    comp = model_compare.compute(_rollup_df(), _decile_port_df(), facts)
    xpm_latest = next(
        c for c in comp.calibration_trend
        if c.campaign_month == "2026-06" and c.model == "XPM"
    )
    assert xpm_latest.available is False


# ============================================================================
# renderer end-to-end
# ============================================================================


def test_renderer_produces_html_with_all_sections():
    """All 7 lettered sections must render even if some are placeholders."""
    from src.ai_agent.facts import ReportPackage
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts)
    mx = mix_analysis.compute(facts)
    st = slice_trends.compute(facts, lookback_months=15)
    bm = big_mac.compute(facts, _cfg()["ai_agent"]["big_mac"])
    combos = combinations.compute(_rollup_df(), _cfg())
    mc = model_catch.compute(facts, mm)
    comp = model_compare.compute(_rollup_df(), _decile_port_df(), facts)
    pkg = ReportPackage(
        facts=facts, mom_yoy=mm, mix=mx, slice_trends=st,
        big_mac=bm, combinations=combos, model_catch=mc, model=comp,
        config_snapshot={"report_title": "Test Title"},
        charts={},
    )
    html = render_html(pkg)
    for letter in "ABCDEF":
        assert f"<h2>{letter}." in html, f"section {letter} missing"
    assert "AI-generated draft" in html
    # Missing charts must surface as placeholders, never as broken <img>.
    assert "chart unavailable" in html
    # Commentary placeholders must be reserved under every chart for Stage 2.
    assert "commentary-label" in html


# ============================================================================
# LLM fallback paths — every failure mode must produce per-slot reasons
# rather than silently dropping the section.
# ============================================================================


def _pkg_for_llm_tests():
    """Build a complete ReportPackage so the section builders have inputs."""
    from src.ai_agent.facts import ReportPackage
    facts = snapshot_builder.build(_rollup_df(), _maturity_full(), _cfg())
    mm = mom_yoy.compute(facts)
    mx = mix_analysis.compute(facts)
    st = slice_trends.compute(facts, lookback_months=15)
    bm = big_mac.compute(facts, _cfg()["ai_agent"]["big_mac"])
    combos = combinations.compute(_rollup_df(), _cfg())
    mc = model_catch.compute(facts, mm)
    comp = model_compare.compute(_rollup_df(), _decile_port_df(), facts)
    return ReportPackage(
        facts=facts, mom_yoy=mm, mix=mx, slice_trends=st,
        big_mac=bm, combinations=combos, model_catch=mc, model=comp,
        config_snapshot={}, charts={},
    )


def test_populate_all_fallback_fills_every_expected_slot():
    """When the LLM client can't even be built, every expected slot gets a
    fallback whose body names the reason."""
    from src.ai_agent.llm.writer import populate_all_fallback
    from src.ai_agent.llm.prompts import get_section_builders
    pkg = _pkg_for_llm_tests()
    reason = "Environment variable OPENAI_API_KEY is not set."

    commentary = populate_all_fallback(pkg, reason)

    # Every slot listed by every builder must be present.
    expected = set()
    for _, builder in get_section_builders(pkg):
        _, _, slot_ids = builder(pkg)
        expected.update(slot_ids)
    assert expected.issubset(commentary.keys()), \
        f"missing fallbacks for: {expected - commentary.keys()}"
    # Each slot must surface the reason in headline AND body.
    for sid, slot in commentary.items():
        assert "unavailable" in slot.headline.lower()
        assert reason in slot.body, f"{sid} body missing reason"
        # Noise chip surfaces the failure at a glance.
        assert any("LLM unavailable" in n for n in slot.noise_flags)


def test_write_commentary_per_section_failure_keeps_others():
    """One LLM call raising must produce fallbacks ONLY for that section's
    expected slots — other sections still render real commentary."""
    from src.ai_agent.llm.writer import write_commentary
    from src.ai_agent.llm.schemas import CommentarySlot, SectionCommentary
    from src.ai_agent.llm.client import LLMError
    pkg = _pkg_for_llm_tests()

    class FlakyClient:
        """Raises only on Section D (so D slots get fallbacks); other sections
        return a stub response with the expected slot_id populated."""
        def generate_structured(self, *, system, user, schema):
            # Detect Section D by a marker we know lives in its user prompt.
            if '"section_id": "D"' in user:
                raise LLMError("simulated transient API error on Section D")
            # For any other section, return a minimal valid SectionCommentary
            # that populates ONE slot id we can find in the user prompt.
            import re
            ids = re.findall(r'"slots_to_populate": \[(.+?)\]', user, re.DOTALL)
            slot_ids = re.findall(r'"([\w_]+)"', ids[0]) if ids else []
            return SectionCommentary(
                section_id="x",
                slots=[CommentarySlot(
                    slot_id=sid,
                    headline=f"stub headline for {sid}",
                    body="stub body " * 5,
                ) for sid in slot_ids],
            )

    commentary = write_commentary(pkg, FlakyClient())

    # Section D slots should all be fallbacks naming the simulated error.
    d_slot_ids = ["section_d_summary", "top_combo_movers"]
    for sid in d_slot_ids:
        assert sid in commentary, f"section D slot {sid} missing"
        assert "unavailable" in commentary[sid].headline.lower()
        assert "simulated transient API error" in commentary[sid].body
    # Other sections should render stub commentary, NOT fallbacks.
    assert "stub headline" in commentary["section_a_summary"].headline


def test_write_commentary_empty_response_triggers_fallback():
    """Model returning zero slots for a section must produce fallbacks for
    every expected slot in that section — no silent gap."""
    from src.ai_agent.llm.writer import write_commentary
    from src.ai_agent.llm.schemas import SectionCommentary
    pkg = _pkg_for_llm_tests()

    class EmptyClient:
        def generate_structured(self, *, system, user, schema):
            return SectionCommentary(section_id="x", slots=[], caveats=[])

    commentary = write_commentary(pkg, EmptyClient())
    # Pick any expected slot — should now be a fallback.
    assert "overall_combo" in commentary
    assert "empty slots list" in commentary["overall_combo"].body


def test_write_commentary_partial_response_fills_missing_slots():
    """Model returning some expected slots but omitting others gets fallbacks
    for the omitted ones."""
    from src.ai_agent.llm.writer import write_commentary
    from src.ai_agent.llm.schemas import CommentarySlot, SectionCommentary
    pkg = _pkg_for_llm_tests()

    class PartialClient:
        """Always returns exactly one slot — whatever the section expects
        first, leaving any second/third expected slot uncovered."""
        def generate_structured(self, *, system, user, schema):
            import re
            ids = re.findall(r'"slots_to_populate": \[(.+?)\]', user, re.DOTALL)
            first_id = re.findall(r'"([\w_]+)"', ids[0])[0] if ids else "missing"
            return SectionCommentary(
                section_id="x",
                slots=[CommentarySlot(
                    slot_id=first_id, headline=f"real headline for {first_id}",
                    body="real body " * 5,
                )],
            )

    commentary = write_commentary(pkg, PartialClient())
    # Section A expects 2 slots; the second should be a fallback.
    # First slot in section_a is section_a_summary, second is overall_combo.
    assert "real headline" in commentary["section_a_summary"].headline
    assert "omitted this slot" in commentary["overall_combo"].body


def test_orchestrator_target_month_clamps_latest_and_maturity(monkeypatch, tmp_path):
    """When the caller pins target_month, ReportFacts.latest_month must
    equal that month — even if newer data exists in the mart — and the
    maturity dict must not contain months past the cutoff."""
    cfg = _cfg()
    cfg["paths"] = {"mart_dir": str(tmp_path),
                    "decile_port_mart_dir": str(tmp_path),
                    "decile_sc_mart_dir": str(tmp_path)}
    cfg["ai_agent"]["llm"] = {"enabled": False, "provider": "stub"}

    from src.ai_agent import orchestrator
    monkeypatch.setattr(orchestrator, "read_mart", lambda _p: _rollup_df())
    monkeypatch.setattr(orchestrator, "read_decile_port_mart",
                        lambda _p: _decile_port_df())
    monkeypatch.setattr(orchestrator, "read_decile_sc_mart", lambda _p: None)
    monkeypatch.setattr(orchestrator.snapshot_builder, "load_maturity",
                        lambda _c: _maturity_full())

    # _rollup_df has 2025-06, 2026-05, 2026-06. Pin latest to 2026-05.
    pkg = orchestrator.build_report_package(cfg, target_month="2026-05")
    assert pkg.facts.latest_month == "2026-05"
    assert "2026-06" not in pkg.facts.months_in_scope
    assert "2026-06" not in pkg.facts.maturity
    # 2026-05 maturity (full per the fixture) must propagate.
    assert pkg.facts.maturity["2026-05"].status == "full"


def test_orchestrator_target_month_none_falls_back_to_latest(monkeypatch, tmp_path):
    """target_month=None must preserve the original 'pick max(months)'
    behavior so existing callers (CLI scripts, default Generate button
    before month-picker) continue to work."""
    cfg = _cfg()
    cfg["paths"] = {"mart_dir": str(tmp_path),
                    "decile_port_mart_dir": str(tmp_path),
                    "decile_sc_mart_dir": str(tmp_path)}
    cfg["ai_agent"]["llm"] = {"enabled": False, "provider": "stub"}
    from src.ai_agent import orchestrator
    monkeypatch.setattr(orchestrator, "read_mart", lambda _p: _rollup_df())
    monkeypatch.setattr(orchestrator, "read_decile_port_mart",
                        lambda _p: _decile_port_df())
    monkeypatch.setattr(orchestrator, "read_decile_sc_mart", lambda _p: None)
    monkeypatch.setattr(orchestrator.snapshot_builder, "load_maturity",
                        lambda _c: _maturity_full())

    pkg = orchestrator.build_report_package(cfg)
    assert pkg.facts.latest_month == "2026-06"  # max of the fixture


def test_dashboard_layout_exposes_month_picker():
    """The AI Report tab layout must include the month dropdown + maturity
    chip span so the new callbacks have outputs to bind to."""
    from dashboard.tab_ai_report import tab_ai_report
    from dash import dcc
    tab = tab_ai_report()
    found = {"dropdown": False, "maturity_span": False, "loading": False}

    def walk(el):
        if isinstance(el, dcc.Dropdown) and getattr(el, "id", None) == "ai-month-select":
            found["dropdown"] = True
        if getattr(el, "id", None) == "ai-month-maturity":
            found["maturity_span"] = True
        if isinstance(el, dcc.Loading):
            found["loading"] = True
        children = getattr(el, "children", None)
        if children is None:
            return
        items = children if isinstance(children, list) else [children]
        for c in items:
            walk(c)

    walk(tab)
    assert found["dropdown"], "month dropdown missing from AI Report layout"
    assert found["maturity_span"], "maturity chip span missing from layout"
    assert found["loading"], "dcc.Loading wrapper still present"


def test_maturity_chip_renders_partial_warning():
    """Selecting a partial month must produce a red-styled chip with
    explicit 'still maturing' wording so the user can't miss the warning."""
    from dashboard.callbacks_ai import _maturity_chip
    chip = _maturity_chip("partial", False)
    # Walk the html.Span to find its text
    text = chip.children if isinstance(chip.children, str) else ""
    assert "partial" in text and "still maturing" in text
    assert chip.style.get("color") == "#9e2a2a", "partial chip must be red-toned"


def test_maturity_chip_renders_full_status():
    """Full-maturity month gets a green chip; XPM availability shown inline."""
    from dashboard.callbacks_ai import _maturity_chip
    chip = _maturity_chip("full", True)
    text = chip.children if isinstance(chip.children, str) else ""
    assert "full" in text and "XPM on" in text
    assert chip.style.get("color") == "#137333"


def test_dispatch_order_b_summary_runs_last_in_b_group():
    """Sprint B: per-dim builders must run BEFORE B-summary so the summary
    call can see their outputs via prior_slots."""
    from src.ai_agent.llm.prompts import get_section_builders
    pkg = _pkg_for_llm_tests()
    ids = [sid for sid, _ in get_section_builders(pkg)]
    # B-summary must appear after EVERY B-{dim}.
    b_dims = [s for s in ids if s.startswith("B-") and s != "B-summary"]
    assert ids.index("B-summary") > max(ids.index(s) for s in b_dims), \
        f"B-summary did not move past per-dim builders: order={ids}"
    # A must still be first; F must still be last.
    assert ids[0] == "A" and ids[-1] == "F"


def test_section_b_summary_payload_includes_per_dim_findings():
    """B-summary builder must read prior_slots and expose per_dim_findings
    in its prompt payload — that's the entire point of the reorder."""
    from src.ai_agent.llm.prompts import section_b_summary
    from src.ai_agent.llm.schemas import CommentarySlot
    pkg = _pkg_for_llm_tests()
    # Seed prior_slots with one per-dim finding the writer would have
    # produced before B-summary's call.
    prior = {
        "slice_annual_fee": CommentarySlot(
            slot_id="slice_annual_fee",
            headline="$95/$95 is the material annual_fee mover (+4.5 pp).",
            body="x" * 50,
            material_movers=["$95/$95 share +4.5 pp"],
            noise_flags=[],
        ),
    }
    _system, user, slot_ids = section_b_summary(pkg, prior)
    assert "per_dim_findings" in user
    assert "annual_fee" in user
    assert "$95/$95" in user, "per-dim headline did not propagate into prompt"
    assert slot_ids == ["section_b_summary"]


def test_section_b_summary_payload_empty_when_no_prior_slots():
    """If called WITHOUT prior_slots (e.g. fallback path), B-summary must
    still build a valid prompt with per_dim_findings empty."""
    from src.ai_agent.llm.prompts import section_b_summary
    pkg = _pkg_for_llm_tests()
    _, user, _ = section_b_summary(pkg, None)
    assert "per_dim_findings" in user      # field present
    assert '"per_dim_findings": {}' in user or '"per_dim_findings": {' in user


# ----- Audit pass -----


def test_auditor_returns_findings_keyed_by_section_letter():
    """The auditor must collapse per-builder ids ('B-summary', 'B-annual_fee'...)
    into top-level letter keys so the template can show one banner per
    visible section."""
    from src.ai_agent.llm.auditor import audit_commentary, AuditIssue, AuditReport
    from src.ai_agent.llm.schemas import CommentarySlot
    pkg = _pkg_for_llm_tests()
    # Build minimal commentary so the auditor has something to review.
    commentary = {}
    from src.ai_agent.llm.prompts import get_section_builders
    for sid, builder in get_section_builders(pkg):
        _, _, slot_ids = builder(pkg, commentary)
        for sid_ in slot_ids:
            commentary.setdefault(sid_, CommentarySlot(
                slot_id=sid_, headline="real headline here for review",
                body="real body content for review " * 4,
            ))

    class FakeAuditClient:
        """Returns one 'error' issue for every section so we can verify
        the keying and pass-through logic."""
        def generate_structured(self, *, system, user, schema):
            # Parse out which section by spotting section_id in the original
            # user prompt (it's there because audit_user includes the full
            # original prompt as 'ORIGINAL FACTS').
            import re
            m = re.search(r'"section_id": "([A-Z\-_a-z]+)"', user)
            sec = m.group(1) if m else "X"
            return AuditReport(
                section_id=sec,
                issues=[AuditIssue(
                    severity="error",
                    issue=f"fake unit bug in {sec}: claims +300 bps for share delta",
                    affected_slot="section_b_summary",
                    suggestion="rewrite as +3.0 pp",
                )],
            )

    findings = audit_commentary(pkg, commentary, FakeAuditClient())
    # Keys must be top-level section letters, not sub-builder ids.
    assert "A" in findings and "C" in findings and "F" in findings
    assert "B-summary" not in findings, "audit findings must collapse to letters"
    assert "B" in findings
    # B should accumulate findings from every B-* sub-builder.
    assert len(findings["B"]) >= 2, "B must aggregate findings from sub-builders"
    # Each finding is an AuditIssue with the expected shape.
    a_issue = findings["A"][0]
    assert a_issue.severity == "error"
    assert "+300 bps" in a_issue.issue


def test_auditor_skips_fallback_slots():
    """Don't waste LLM spend auditing slots that are themselves fallback
    notices — the fallback IS the audit verdict for that slot."""
    from src.ai_agent.llm.auditor import audit_commentary
    from src.ai_agent.llm.writer import _fallback_slot
    pkg = _pkg_for_llm_tests()
    # Every slot is a fallback.
    commentary = {}
    from src.ai_agent.llm.prompts import get_section_builders
    for sid, builder in get_section_builders(pkg):
        _, _, slot_ids = builder(pkg, commentary)
        for sid_ in slot_ids:
            commentary[sid_] = _fallback_slot(sid_, "api key missing")

    called: list[str] = []

    class TrackingClient:
        def generate_structured(self, *, system, user, schema):
            called.append(user[:80])
            raise AssertionError("auditor should NOT have called the LLM")

    findings = audit_commentary(pkg, commentary, TrackingClient())
    assert findings == {}, "no findings expected when nothing reviewable"
    assert called == [], "audit must skip fallback-only sections"


def test_auditor_failure_records_info_issue():
    """When the audit call itself fails, the report still renders — and
    we surface the audit failure as an info-severity note rather than
    silently dropping it."""
    from src.ai_agent.llm.auditor import audit_commentary
    from src.ai_agent.llm.client import LLMError
    from src.ai_agent.llm.schemas import CommentarySlot
    pkg = _pkg_for_llm_tests()
    commentary = {}
    from src.ai_agent.llm.prompts import get_section_builders
    for sid, builder in get_section_builders(pkg):
        _, _, slot_ids = builder(pkg, commentary)
        for sid_ in slot_ids:
            commentary.setdefault(sid_, CommentarySlot(
                slot_id=sid_, headline="headline placeholder for review",
                body="body placeholder for review " * 4,
            ))

    class AlwaysFailClient:
        def generate_structured(self, *, system, user, schema):
            raise LLMError("simulated audit-pass network failure")

    findings = audit_commentary(pkg, commentary, AlwaysFailClient())
    # Every section letter that had reviewable content gets exactly one info
    # banner per failed sub-builder call — never errors out the report.
    assert findings, "expected info-severity audit-unavailable notes"
    for letter, issues in findings.items():
        for iss in issues:
            assert iss.severity == "info"
            assert "could not complete" in iss.issue or "crashed" in iss.issue


def test_orchestrator_skips_audit_when_disabled(monkeypatch, tmp_path):
    """ai_agent.llm.audit_enabled: false must skip the audit step entirely
    — verified by checking that audit_findings stays empty even when LLM
    is otherwise enabled and would produce commentary."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = _cfg()
    cfg["ai_agent"]["llm"] = {
        "provider": "openai", "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY", "timeout_seconds": 30,
        "enabled": True, "audit_enabled": False,    # opt-out
    }
    cfg["paths"] = {"mart_dir": str(tmp_path),
                    "decile_port_mart_dir": str(tmp_path),
                    "decile_sc_mart_dir": str(tmp_path)}
    from src.ai_agent import orchestrator
    monkeypatch.setattr(orchestrator, "read_mart", lambda _p: _rollup_df())
    monkeypatch.setattr(orchestrator, "read_decile_port_mart",
                        lambda _p: _decile_port_df())
    monkeypatch.setattr(orchestrator, "read_decile_sc_mart", lambda _p: None)
    monkeypatch.setattr(orchestrator.snapshot_builder, "load_maturity",
                        lambda _c: _maturity_full())

    pkg = orchestrator.build_report_package(cfg)
    # Commentary populated via fallback path (no API key); audit skipped.
    assert pkg.commentary, "fallback commentary should still be populated"
    assert pkg.audit_findings == {}, "audit must be skipped when disabled"


def test_orchestrator_with_missing_api_key_does_not_raise(monkeypatch, tmp_path):
    """When ai_agent.llm.enabled=true but the API key env var is unset and
    provider=openai, the orchestrator must NOT raise — it must populate
    fallback commentary for every slot so the report still renders."""
    # Force-build a config that requests openai without the key set.
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_REPORT_LLM_PROVIDER", raising=False)

    cfg = _cfg()
    cfg["ai_agent"]["llm"] = {
        "provider": "openai",
        "model": "gpt-4o",
        "api_key_env": "OPENAI_API_KEY",
        "timeout_seconds": 30,
        "enabled": True,
    }

    # Mock the mart readers since orchestrator pulls from disk paths.
    from src.ai_agent import orchestrator
    monkeypatch.setattr(orchestrator, "read_mart", lambda _p: _rollup_df())
    monkeypatch.setattr(orchestrator, "read_decile_port_mart",
                        lambda _p: _decile_port_df())
    monkeypatch.setattr(orchestrator, "read_decile_sc_mart", lambda _p: None)
    monkeypatch.setattr(orchestrator.snapshot_builder, "load_maturity",
                        lambda _c: _maturity_full())
    cfg["paths"] = {"mart_dir": str(tmp_path), "decile_port_mart_dir": str(tmp_path),
                    "decile_sc_mart_dir": str(tmp_path)}

    pkg = orchestrator.build_report_package(cfg)

    # No exception → graceful. Every slot must have fallback content.
    assert pkg.commentary, "commentary dict should be populated with fallbacks"
    sample = next(iter(pkg.commentary.values()))
    assert "OPENAI_API_KEY" in sample.body, \
        "fallback should name the missing env var"


# ----------------------------------------------------------------- helpers


def _is_empty_trend(rows) -> bool:
    return not rows or all(r.volume in (None, 0) for r in rows)
