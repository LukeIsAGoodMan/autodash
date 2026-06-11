"""Top-level pipeline: marts → ReportPackage.

Reads all parquet/csv inputs once, then hands pure DataFrames to each
analysis module. The renderer is invoked separately so callers can either
consume the package object (Dash callback) or write HTML to disk (CLI).
"""
from __future__ import annotations

from src.build_mart import read_mart
from src.ingest_decile import read_decile_port_mart, read_decile_sc_mart

from . import mom_yoy, model_compare, snapshot_builder
from .facts import ReportPackage


def build_report_package(cfg: dict) -> ReportPackage:
    rollup_df = read_mart(cfg["paths"]["mart_dir"])
    decile_port = read_decile_port_mart(cfg["paths"]["decile_port_mart_dir"])
    decile_sc = read_decile_sc_mart(cfg["paths"]["decile_sc_mart_dir"])  # noqa: F841 -- reserved for per-scorecard rank-order section in next iteration
    maturity = snapshot_builder.load_maturity(cfg)

    facts = snapshot_builder.build(rollup_df, maturity, cfg)
    mm = mom_yoy.compute(facts)
    model = model_compare.compute(rollup_df, decile_port, facts)

    return ReportPackage(
        facts=facts,
        mom_yoy=mm,
        model=model,
        config_snapshot={
            "small_cell_threshold": cfg.get("dashboard", {}).get("small_cell_threshold"),
            "maturity_threshold_months": cfg.get("mart", {}).get("maturity_threshold_months"),
            "report_title": cfg.get("dashboard", {}).get("title"),
        },
    )
