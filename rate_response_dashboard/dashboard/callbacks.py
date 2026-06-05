"""Dash callbacks. Every callback reads the parquet mart via a cached loader,
applies filters, then delegates aggregation to src.metrics.

We register callbacks against an app passed in, instead of importing the app
at module load time, so app.py stays the single source of truth.
"""
from __future__ import annotations

import io
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Iterable

import pandas as pd
import plotly.express as px
import polars as pl
from dash import Input, Output, State, dash_table, dcc, no_update

from src import metrics
from src.build_mart import list_partition_months, read_mart

PCT_METRICS = {
    "actual_response_rate", "expected_rr_trm", "expected_rr_xpm", "board_rate",
}
RATIO_METRICS = {"actual_vs_expected_trm", "actual_vs_expected_xpm"}
COUNT_METRICS = {"volume", "responders", "Boards"}


# ----------------------------------------------------------- mart cache
@lru_cache(maxsize=1)
def _cached_mart(mart_dir: str, mart_version: float) -> pl.DataFrame:
    """Mart cache keyed by directory + a 'version' (we pass max mtime).

    The lru_cache is global and lives for the process lifetime. Rebuild
    happens automatically when a partition mtime changes.
    """
    return read_mart(mart_dir)


def _mart_version(mart_dir: str) -> float:
    """Cheap cache key: most recent partition mtime."""
    p = Path(mart_dir)
    mtimes = [f.stat().st_mtime for f in p.glob("campaign_month=*/rollup.parquet")]
    return max(mtimes) if mtimes else 0.0


def load_mart(cfg: dict) -> pl.DataFrame:
    mart_dir = cfg["paths"]["mart_dir"]
    return _cached_mart(mart_dir, _mart_version(mart_dir))


# ----------------------------------------------------------- filter helpers
def _apply_filters(df: pl.DataFrame, filters: dict) -> pl.DataFrame:
    out = df
    for col, values in filters.items():
        if not values:
            continue
        if col not in out.columns:
            continue
        out = out.filter(pl.col(col).is_in(list(values)))
    return out


def _options(df: pl.DataFrame, col: str) -> list[dict]:
    if col not in df.columns:
        return []
    vals = df[col].drop_nulls().unique().sort().to_list()
    return [{"label": str(v), "value": v} for v in vals]


def _fmt_count(v) -> str:
    if v is None:
        return "—"
    return f"{int(v):,}"


def _fmt_pct(v) -> str:
    if v is None:
        return "—"
    return f"{v * 100:.2f}%"


def _fmt_ratio(v) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"


def _fmt_metric(metric: str, v) -> str:
    if metric in COUNT_METRICS:
        return _fmt_count(v)
    if metric in PCT_METRICS:
        return _fmt_pct(v)
    if metric in RATIO_METRICS:
        return _fmt_ratio(v)
    return str(v) if v is not None else "—"


# ----------------------------------------------------------- registration
def register_callbacks(app, cfg: dict) -> None:
    # ---------------------- header ----------------------
    @app.callback(
        Output("subtitle-latest-month", "children"),
        Output("subtitle-last-refresh", "children"),
        Input("tabs", "active_tab"),
    )
    def _header(_tab):
        months = list_partition_months(cfg["paths"]["mart_dir"])
        latest = months[-1] if months else "—"
        v = _mart_version(cfg["paths"]["mart_dir"])
        ts = datetime.fromtimestamp(v).isoformat(timespec="seconds") if v else "—"
        return latest, ts

    # ---------------------- exec tab KPIs ----------------
    @app.callback(
        Output("kpi-volume", "children"),
        Output("kpi-responders", "children"),
        Output("kpi-boards", "children"),
        Output("kpi-arr", "children"),
        Output("kpi-exp-trm", "children"),
        Output("kpi-exp-xpm", "children"),
        Output("kpi-aoe-trm", "children"),
        Output("kpi-board-rate", "children"),
        Output("exec-trend-chart", "figure"),
        Input("tabs", "active_tab"),
    )
    def _exec(tab):
        df = load_mart(cfg)
        if df.is_empty():
            blank = {"data": [], "layout": {"title": "No data"}}
            return ("—",) * 8 + (blank,)

        k = metrics.kpi_totals(df)
        trend = metrics.monthly_trend(df).to_pandas().sort_values("campaign_month")

        fig = px.line(
            trend.melt(
                id_vars="campaign_month",
                value_vars=["actual_response_rate", "expected_rr_trm",
                            "expected_rr_xpm", "board_rate"],
                var_name="metric", value_name="value",
            ),
            x="campaign_month", y="value", color="metric",
            markers=True,
        )
        fig.update_layout(
            margin=dict(l=8, r=8, t=24, b=8),
            yaxis_tickformat=".1%",
            legend_title="",
            template="plotly_white",
        )
        return (
            _fmt_count(k["volume"]),
            _fmt_count(k["responders"]),
            _fmt_count(k["Boards"]),
            _fmt_pct(k["actual_response_rate"]),
            _fmt_pct(k["expected_rr_trm"]),
            _fmt_pct(k["expected_rr_xpm"]),
            _fmt_ratio(k["actual_vs_expected_trm"]),
            _fmt_pct(k["board_rate"]),
            fig,
        )

    # ---------------------- populate filter options ------
    _filter_cols = {
        "f-months": "campaign_month",
        "f-vs": "vs_band",
        "f-scorecard": "scorecard",
        "f-prospect": "Prospect_type",
        "f-rm": "rm_flag",
        "f-trm": "trm10_tier",
        "f-fee": "annual_fee",
    }

    for prefix in ("pivot", "export"):
        for suffix, col in _filter_cols.items():
            cid = f"{prefix}-{suffix}"

            @app.callback(Output(cid, "options"), Input("tabs", "active_tab"))
            def _opts(_tab, _col=col):
                return _options(load_mart(cfg), _col)

    # ---------------------- pivot table ------------------
    @app.callback(
        Output("pivot-table", "data"),
        Output("pivot-table", "columns"),
        Input("pivot-row-dim", "value"),
        Input("pivot-metric", "value"),
        Input("pivot-suppress", "value"),
        Input("pivot-format", "value"),
        Input("pivot-f-months", "value"),
        Input("pivot-f-vs", "value"),
        Input("pivot-f-scorecard", "value"),
        Input("pivot-f-prospect", "value"),
        Input("pivot-f-rm", "value"),
        Input("pivot-f-trm", "value"),
        Input("pivot-f-fee", "value"),
    )
    def _pivot(row_dim, metric, suppress, fmt,
               f_months, f_vs, f_scorecard, f_prospect, f_rm, f_trm, f_fee):
        df = load_mart(cfg)
        df = _apply_filters(df, {
            "campaign_month": f_months, "vs_band": f_vs,
            "scorecard": f_scorecard, "Prospect_type": f_prospect,
            "rm_flag": f_rm, "trm10_tier": f_trm, "annual_fee": f_fee,
        })
        if df.is_empty() or row_dim is None or metric is None:
            return [], []

        # Aggregate at the cell level first so suppression considers cell volume.
        long = metrics.aggregate_by(df, [row_dim, "campaign_month"])
        long = metrics.suppress_small_cells(long, threshold=suppress or 0)

        # Pivot to wide form on the chosen metric.
        wide = long.to_pandas().pivot_table(
            index=row_dim, columns="campaign_month",
            values=metric, aggfunc="first",
        ).reset_index()
        wide.columns = [str(c) for c in wide.columns]

        # Format values according to metric type.
        for c in wide.columns[1:]:
            wide[c] = wide[c].apply(lambda v: _fmt_metric(metric, v) if fmt == "pct"
                                    or metric in COUNT_METRICS
                                    else (f"{v:.4f}" if v is not None and not pd.isna(v) else "—"))

        columns = [{"name": c, "id": c} for c in wide.columns]
        return wide.to_dict("records"), columns

    # ---------------------- model performance ------------
    @app.callback(
        Output("model-arr-vs-exp", "figure"),
        Output("model-by-trm", "figure"),
        Output("model-by-scorecard", "figure"),
        Input("tabs", "active_tab"),
    )
    def _model(_tab):
        df = load_mart(cfg)
        if df.is_empty():
            blank = {"data": [], "layout": {"title": "No data"}}
            return blank, blank, blank

        by_month = metrics.monthly_trend(df).to_pandas().sort_values("campaign_month")
        f1 = px.line(
            by_month.melt(
                id_vars="campaign_month",
                value_vars=["actual_response_rate", "expected_rr_trm"],
                var_name="series", value_name="rate",
            ),
            x="campaign_month", y="rate", color="series", markers=True,
        )
        f1.update_layout(template="plotly_white", yaxis_tickformat=".1%",
                         margin=dict(l=8, r=8, t=24, b=8))

        by_trm = metrics.aggregate_by(df, ["trm10_tier"]).to_pandas().sort_values("trm10_tier")
        f2 = px.bar(by_trm, x="trm10_tier", y="actual_response_rate",
                    text="actual_response_rate")
        f2.update_traces(texttemplate="%{text:.2%}", textposition="outside")
        f2.update_layout(template="plotly_white", yaxis_tickformat=".1%",
                         margin=dict(l=8, r=8, t=24, b=8))

        by_sc = metrics.aggregate_by(df, ["scorecard"]).to_pandas().sort_values("scorecard")
        f3 = px.bar(by_sc, x="scorecard", y="actual_vs_expected_trm",
                    text="actual_vs_expected_trm")
        f3.update_traces(texttemplate="%{text:.2f}", textposition="outside")
        f3.update_layout(template="plotly_white",
                         margin=dict(l=8, r=8, t=24, b=8))
        return f1, f2, f3

    # ---------------------- data quality tab -------------
    @app.callback(
        Output("dq-latest", "children"),
        Output("dq-last-refresh", "children"),
        Output("dq-partition-count", "children"),
        Output("dq-table", "data"),
        Output("dq-table", "columns"),
        Input("tabs", "active_tab"),
    )
    def _dq(_tab):
        months = list_partition_months(cfg["paths"]["mart_dir"])
        v = _mart_version(cfg["paths"]["mart_dir"])
        ts = datetime.fromtimestamp(v).isoformat(timespec="seconds") if v else "—"

        summary_path = Path(cfg["paths"]["logs_dir"]) / "validation_summary.csv"
        if summary_path.exists():
            pdf = pd.read_csv(summary_path)
            cols = [{"name": c, "id": c} for c in pdf.columns]
            data = pdf.to_dict("records")
        else:
            cols, data = [], []
        return (months[-1] if months else "—", ts, _fmt_count(len(months)),
                data, cols)

    # ---------------------- export -----------------------
    @app.callback(
        Output("export-download", "data"),
        Input("btn-export-csv", "n_clicks"),
        Input("btn-export-xlsx", "n_clicks"),
        State("export-f-months", "value"),
        State("export-f-vs", "value"),
        State("export-f-scorecard", "value"),
        State("export-f-prospect", "value"),
        State("export-f-rm", "value"),
        State("export-f-trm", "value"),
        State("export-f-fee", "value"),
        prevent_initial_call=True,
    )
    def _export(n_csv, n_xlsx,
                f_months, f_vs, f_scorecard, f_prospect, f_rm, f_trm, f_fee):
        from dash import ctx
        if not ctx.triggered_id:
            return no_update
        df = _apply_filters(load_mart(cfg), {
            "campaign_month": f_months, "vs_band": f_vs,
            "scorecard": f_scorecard, "Prospect_type": f_prospect,
            "rm_flag": f_rm, "trm10_tier": f_trm, "annual_fee": f_fee,
        }).to_pandas()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if ctx.triggered_id == "btn-export-csv":
            return dcc.send_data_frame(df.to_csv, f"rate_response_{ts}.csv", index=False)
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as w:
            df.to_excel(w, index=False, sheet_name="rollup")
        return dcc.send_bytes(buf.getvalue(), f"rate_response_{ts}.xlsx")
