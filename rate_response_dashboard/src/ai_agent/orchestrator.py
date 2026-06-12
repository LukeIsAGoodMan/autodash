"""Top-level pipeline: marts → ReportPackage.

Reads all parquet/csv inputs once, then hands pure DataFrames to each
analysis module. Charts are built last (after all analysis is done) so the
chart layer can compose multiple fact bundles into a single figure.
"""
from __future__ import annotations

from src.build_mart import read_mart
from src.ingest_decile import read_decile_port_mart, read_decile_sc_mart

from . import big_mac, combinations, mix_analysis, model_catch, model_compare, mom_yoy, slice_trends, snapshot_builder
from .facts import ReportPackage
from .llm import factory as llm_factory
from .llm import writer as llm_writer
from .llm.client import LLMError
from .report import chart_builder


log = __import__("logging").getLogger(__name__)


def build_report_package(cfg: dict) -> ReportPackage:
    rollup_df = read_mart(cfg["paths"]["mart_dir"])
    decile_port = read_decile_port_mart(cfg["paths"]["decile_port_mart_dir"])
    _ = read_decile_sc_mart(cfg["paths"]["decile_sc_mart_dir"])  # reserved
    maturity = snapshot_builder.load_maturity(cfg)
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
    if (ai_cfg.get("llm") or {}).get("enabled", False):
        try:
            client = llm_factory.build_client(cfg)
        except LLMError as e:
            log.warning("LLM client unavailable, every slot will show fallback: %s", e)
            pkg.commentary = llm_writer.populate_all_fallback(pkg, str(e))
        else:
            pkg.commentary = llm_writer.write_commentary(pkg, client)

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
