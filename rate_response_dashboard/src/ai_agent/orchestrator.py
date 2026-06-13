"""Top-level pipeline: marts → ReportPackage.

Reads all parquet/csv inputs once, then hands pure DataFrames to each
analysis module. Charts are built last (after all analysis is done) so the
chart layer can compose multiple fact bundles into a single figure.
"""
from __future__ import annotations

import polars as pl

from src.build_mart import read_mart
from src.ingest_decile import read_decile_port_mart, read_decile_sc_mart

from . import big_mac, combinations, mix_analysis, model_catch, model_compare, mom_yoy, slice_trends, snapshot_builder
from .facts import ReportPackage
from .llm import auditor as llm_auditor
from .llm import factory as llm_factory
from .llm import writer as llm_writer
from .llm.client import LLMError
from .report import chart_builder


log = __import__("logging").getLogger(__name__)


def build_report_package(cfg: dict, target_month: str | None = None) -> ReportPackage:
    """Build the report.

    `target_month` lets the caller pick the report's "as-of" month
    (format YYYY-MM). The mart, decile mart, and maturity dict are
    truncated to months `<= target_month` so every downstream analysis —
    MoM/YoY anchors, lookback windows, chart trends — treats the chosen
    month as the latest. When None (default), the latest month available
    in the mart is used, matching the original behavior.
    """
    rollup_df = read_mart(cfg["paths"]["mart_dir"])
    decile_port = read_decile_port_mart(cfg["paths"]["decile_port_mart_dir"])
    _ = read_decile_sc_mart(cfg["paths"]["decile_sc_mart_dir"])  # reserved
    maturity = snapshot_builder.load_maturity(cfg)

    if target_month:
        # Pin the report's latest month to the user-chosen one by dropping
        # any data beyond it. Everything downstream auto-derives "latest"
        # from max(months) so no other module needs to know.
        if "campaign_month" in rollup_df.columns:
            rollup_df = rollup_df.filter(pl.col("campaign_month") <= target_month)
        if decile_port is not None and "campaign_month" in decile_port.columns:
            decile_port = decile_port.filter(pl.col("campaign_month") <= target_month)
        maturity = {m: info for m, info in maturity.items() if m <= target_month}

    ai_cfg = cfg.get("ai_agent", {})

    facts = snapshot_builder.build(rollup_df, maturity, cfg)
    mm = mom_yoy.compute(facts)
    mix = mix_analysis.compute(facts)
    slice_bundle = slice_trends.compute(facts, lookback_months=int(ai_cfg.get("chart_lookback_months", 15)))
    bm = big_mac.compute(facts, ai_cfg.get("big_mac", {}))
    combos = combinations.compute(rollup_df, cfg)
    catch = model_catch.compute(facts, mm)
    model = model_compare.compute(rollup_df, decile_port, facts)

    charts = _build_charts(facts, mix, slice_bundle, bm, combos, model, ai_cfg)

    pkg = ReportPackage(
        facts=facts,
        mom_yoy=mm,
        mix=mix,
        slice_trends=slice_bundle,
        big_mac=bm,
        combinations=combos,
        model_catch=catch,
        model=model,
        config_snapshot={
            "small_cell_threshold": cfg.get("dashboard", {}).get("small_cell_threshold"),
            "maturity_threshold_months": cfg.get("mart", {}).get("maturity_threshold_months"),
            "report_title": cfg.get("dashboard", {}).get("title"),
            "big_mac": ai_cfg.get("big_mac"),
            "llm_provider": (ai_cfg.get("llm") or {}).get("provider"),
        },
        charts=charts,
    )

    # Stage 2 LLM commentary. Skipped entirely when ai_agent.llm.enabled
    # is false — pipeline stays Stage-1-compatible. When enabled, never
    # raise out of this block: a missing API key, a network outage, or
    # an empty model response all get translated into per-slot fallback
    # commentary that names the reason in the rendered report.
    llm_cfg = ai_cfg.get("llm") or {}
    if llm_cfg.get("enabled", False):
        try:
            client = llm_factory.build_client(cfg)
        except LLMError as e:
            log.warning("LLM client unavailable, every slot will show fallback: %s", e)
            pkg.commentary = llm_writer.populate_all_fallback(pkg, str(e))
        else:
            pkg.commentary = llm_writer.write_commentary(pkg, client)
            # Sprint B: independent audit pass over the writer's output.
            # Same LLM endpoint, fresh system prompt that frames the model
            # as a reviewer rather than an author. Costs ~1 extra call per
            # section; disable via ai_agent.llm.audit_enabled: false if
            # spend matters more than quality control.
            if llm_cfg.get("audit_enabled", True):
                pkg.audit_findings = llm_auditor.audit_commentary(
                    pkg, pkg.commentary, client,
                )
                # Tier-2 reducer — needs per-section findings as input, so
                # only runs when audit_enabled is on. Adds ~1 LLM call per
                # report (compressed payload, ~5-10K input tokens).
                if llm_cfg.get("global_audit_enabled", True):
                    pkg.global_audit = llm_auditor.audit_global(
                        pkg, pkg.commentary, pkg.audit_findings, client,
                    )

    return pkg


def _build_charts(facts, mix, slice_bundle, bm, combos, model, ai_cfg) -> dict[str, str]:
    """Each chart is independent — one failure does not fail the report.

    Combo charts (stacked-bar volume by dim_value + overlay rate line) are
    the primary visualization, matching the pivot view in the dashboard.
    """
    raw = {
        "overall_combo": chart_builder.overall_combo(facts),
        "big_mac_overall": chart_builder.big_mac_overall_combo(bm),
        "big_mac_drill": chart_builder.big_mac_drill_trend(bm),
        "calibration": chart_builder.calibration_trend(model.calibration_trend),
        "top_combo_movers": chart_builder.top_combo_movers_chart(combos),
    }
    for dim, rows in slice_bundle.by_dim.items():
        raw[f"slice_{dim}"] = chart_builder.slice_combo(rows, dim, facts)
    return {k: v for k, v in raw.items() if v}
