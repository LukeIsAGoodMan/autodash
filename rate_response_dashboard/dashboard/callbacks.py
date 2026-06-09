"""Dash callbacks. All read the parquet mart via a cached loader, apply
filters, then delegate aggregation to src.metrics.

Major change vs the first draft:
  - Executive Summary now returns 16 KPI values (Latest + Overall pair per card).
  - Pivot returns a table (with Overall row+column) PLUS a combo chart
    (stacked-bar volume mix + line metric trend).
  - Model Performance has its own filter block and a new by-vs_band chart.
"""
from __future__ import annotations

import io
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import polars as pl
from dash import Input, Output, State, dash_table, dcc, no_update

from src import metrics
from src.build_mart import list_partition_months, read_mart

PCT_METRICS = {
    "actual_response_rate", "actual_board_rate",
    "expected_rr_trm", "expected_rr_xpm",
}
RATIO_METRICS = {"actual_vs_expected_trm", "actual_vs_expected_xpm"}
COUNT_METRICS = {"volume", "responders", "Boards"}

# Palette kept in sync with plotly_template.OMNI_PALETTE and assets/custom.css.
PRIMARY = "#1a4d8c"          # --omni
ACCENT = "#4a7bb7"           # --accent
GOOD = "#2e7a52"             # --good
WARN = "#c98a25"             # --warn
BAD = "#b3434a"              # --bad

PALETTE = [
    "#1a4d8c", "#4a7bb7", "#2e7a52", "#c98a25", "#b3434a",
    "#6b7d92", "#7e9bc0", "#4f6378", "#a9b8c8", "#384a5e",
]


# ----------------------------------------------------------- mart cache
@lru_cache(maxsize=1)
def _cached_mart(mart_dir: str, mart_version: float) -> pl.DataFrame:
    return read_mart(mart_dir)


def _mart_version(mart_dir: str) -> float:
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
    try:
        return f"{int(v):,}"
    except (TypeError, ValueError):
        return "—"


def _fmt_pct(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "—"
    return f"{v * 100:.2f}%"


def _fmt_ratio(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
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


def _latest_month(df: pl.DataFrame) -> str | None:
    if df.is_empty() or "campaign_month" not in df.columns:
        return None
    return df["campaign_month"].max()


# ----------------------------------------------------------- registration
def register_callbacks(app, cfg: dict) -> None:

    # ----------------------------------------------------------- hero
    @app.callback(
        Output("hero-latest", "children"),
        Output("hero-months", "children"),
        Output("hero-refresh", "children"),
        Output("hero-verdict-text", "children"),
        Input("tabs", "active_tab"),
    )
    def _hero(_tab):
        months = list_partition_months(cfg["paths"]["mart_dir"])
        latest = months[-1] if months else "—"
        v = _mart_version(cfg["paths"]["mart_dir"])
        ts = datetime.fromtimestamp(v).isoformat(timespec="seconds") if v else "—"
        verdict = "data fresh" if months else "no data"
        return latest, _fmt_count(len(months)), ts, verdict

    # ----------------------------------------------------------- executive
    @app.callback(
        Output("exec-latest-month-badge", "children"),
        Output("kpi-vol-latest", "children"),
        Output("kpi-vol-overall", "children"),
        Output("kpi-resp-latest", "children"),
        Output("kpi-resp-overall", "children"),
        Output("kpi-boards-latest", "children"),
        Output("kpi-boards-overall", "children"),
        Output("kpi-abr-latest", "children"),
        Output("kpi-abr-overall", "children"),
        Output("kpi-arr-latest", "children"),
        Output("kpi-arr-overall", "children"),
        Output("kpi-trm-latest", "children"),
        Output("kpi-trm-overall", "children"),
        Output("kpi-xpm-latest", "children"),
        Output("kpi-xpm-overall", "children"),
        Output("kpi-aoe-latest", "children"),
        Output("kpi-aoe-overall", "children"),
        Output("exec-trend-chart", "figure"),
        Input("tabs", "active_tab"),
    )
    def _exec(_tab):
        df = load_mart(cfg)
        if df.is_empty():
            blank = {"data": [], "layout": {"title": "No data"}}
            return ("—",) + ("—",) * 16 + (blank,)

        latest_m = _latest_month(df)
        latest_df = df.filter(pl.col("campaign_month") == latest_m)
        k_latest = metrics.kpi_totals(latest_df)
        k_all = metrics.kpi_totals(df)

        def L(key, fmt):
            return fmt(k_latest.get(key))

        def O(key, fmt):
            return fmt(k_all.get(key))

        trend = metrics.monthly_trend(df).to_pandas().sort_values("campaign_month")
        long = trend.melt(
            id_vars="campaign_month",
            value_vars=["actual_board_rate", "actual_response_rate",
                        "expected_rr_trm", "expected_rr_xpm"],
            var_name="metric", value_name="value",
        )
        fig = px.line(
            long, x="campaign_month", y="value", color="metric",
            markers=True,
            color_discrete_sequence=PALETTE,
        )
        fig.update_layout(
            yaxis_tickformat=".1%",
            yaxis_title=None,
            xaxis_title=None,
            legend_title="",
            height=380,
        )
        fig.update_traces(line=dict(width=2.5), marker=dict(size=7,
                                                            line=dict(color="#ffffff", width=1.5)))

        return (
            latest_m or "—",
            L("volume", _fmt_count), O("volume", _fmt_count),
            L("responders", _fmt_count), O("responders", _fmt_count),
            L("Boards", _fmt_count), O("Boards", _fmt_count),
            L("actual_board_rate", _fmt_pct), O("actual_board_rate", _fmt_pct),
            L("actual_response_rate", _fmt_pct), O("actual_response_rate", _fmt_pct),
            L("expected_rr_trm", _fmt_pct), O("expected_rr_trm", _fmt_pct),
            L("expected_rr_xpm", _fmt_pct), O("expected_rr_xpm", _fmt_pct),
            L("actual_vs_expected_trm", _fmt_ratio), O("actual_vs_expected_trm", _fmt_ratio),
            fig,
        )

    # ----------------------------------------------------------- filter options
    _filter_cols = {
        "f-months": "campaign_month",
        "f-vs": "vs_band",
        "f-scorecard": "scorecard",
        "f-prospect": "Prospect_type",
        "f-rm": "rm_flag",
        "f-trm": "trm10_tier",
        "f-fee": "annual_fee",
    }

    for prefix in ("pivot", "model", "export"):
        for suffix, col in _filter_cols.items():
            cid = f"{prefix}-{suffix}"

            @app.callback(Output(cid, "options"), Input("tabs", "active_tab"))
            def _opts(_tab, _col=col):
                return _options(load_mart(cfg), _col)

    # ----------------------------------------------------------- pivot
    @app.callback(
        Output("pivot-table", "data"),
        Output("pivot-table", "columns"),
        Output("pivot-combo-chart", "figure"),
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
        empty_fig = {"data": [], "layout": {"title": "No data"}}
        if df.is_empty() or row_dim is None or metric is None:
            return [], [], empty_fig

        # 1) Per-cell aggregation: each row_dim x month combination, then mask
        #    small cells. Suppression nulls the rate metric only.
        cell = metrics.aggregate_by(df, [row_dim, "campaign_month"])
        cell_masked = metrics.suppress_small_cells(cell, threshold=suppress or 0)

        # 2) Build the wide pivot from masked metric values.
        wide_metric = (
            cell_masked.to_pandas()
            .pivot_table(index=row_dim, columns="campaign_month",
                         values=metric, aggfunc="first")
            .reset_index()
        )
        # 3) Overall column = recompute metric across all months for each row_dim
        row_total = metrics.aggregate_by(df, [row_dim])
        row_total_masked = metrics.suppress_small_cells(row_total, threshold=suppress or 0)
        wide_metric = wide_metric.merge(
            row_total_masked.select([row_dim, metric]).to_pandas()
                            .rename(columns={metric: "Overall"}),
            on=row_dim, how="left",
        )

        # 4) Overall row = recompute metric across all row_dim values per month
        month_total = metrics.aggregate_by(df, ["campaign_month"])
        overall_row = month_total.to_pandas().set_index("campaign_month")[metric].to_dict()
        overall_total = metrics.kpi_totals(df).get(metric)
        overall_row[row_dim] = "Overall"
        overall_row["Overall"] = overall_total
        wide_metric = pd.concat([wide_metric, pd.DataFrame([overall_row])],
                                ignore_index=True)

        # 5) Rename row_dim column to "Row" for stable DataTable referencing
        wide_metric = wide_metric.rename(columns={row_dim: "Row"})
        wide_metric.columns = [str(c) for c in wide_metric.columns]

        # 6) Format values per metric type
        for c in wide_metric.columns[1:]:
            wide_metric[c] = wide_metric[c].apply(lambda v: _fmt_metric(metric, v))

        columns = [{"name": c, "id": c} for c in wide_metric.columns]
        data = wide_metric.to_dict("records")

        # 7) Combo chart: stacked bars of volume by row_dim per month + line
        #    of the selected metric per month (secondary axis).
        vol_pivot = (
            df.group_by([row_dim, "campaign_month"])
              .agg(pl.col("volume").sum().alias("volume"))
              .sort([row_dim, "campaign_month"])
              .to_pandas()
        )
        fig = go.Figure()
        for i, dim_val in enumerate(sorted(vol_pivot[row_dim].dropna().unique(),
                                            key=lambda v: str(v))):
            sub = vol_pivot[vol_pivot[row_dim] == dim_val].sort_values("campaign_month")
            fig.add_bar(
                x=sub["campaign_month"], y=sub["volume"],
                name=str(dim_val),
                marker_color=PALETTE[i % len(PALETTE)],
            )

        m_pdf = month_total.to_pandas().sort_values("campaign_month")
        fig.add_scatter(
            x=m_pdf["campaign_month"], y=m_pdf[metric],
            yaxis="y2", mode="lines+markers",
            name=metric,
            line=dict(color=BAD, width=3),
            marker=dict(size=8, line=dict(color="#ffffff", width=1.8)),
        )

        y2_fmt = ".1%" if metric in PCT_METRICS else (
            ".2f" if metric in RATIO_METRICS else None
        )
        fig.update_layout(
            barmode="stack",
            xaxis_title=None,
            yaxis=dict(title="Volume", tickformat=","),
            yaxis2=dict(title=metric, overlaying="y", side="right",
                        tickformat=y2_fmt, showgrid=False),
            height=440,
        )
        return data, columns, fig

    # ----------------------------------------------------------- model perf
    @app.callback(
        Output("model-by-vsband", "figure"),
        Output("model-arr-vs-exp", "figure"),
        Output("model-by-trm", "figure"),
        Output("model-by-scorecard", "figure"),
        Input("model-f-months", "value"),
        Input("model-f-vs", "value"),
        Input("model-f-scorecard", "value"),
        Input("model-f-prospect", "value"),
        Input("model-f-rm", "value"),
        Input("model-f-trm", "value"),
        Input("model-f-fee", "value"),
    )
    def _model(f_months, f_vs, f_scorecard, f_prospect, f_rm, f_trm, f_fee):
        df = load_mart(cfg)
        df = _apply_filters(df, {
            "campaign_month": f_months, "vs_band": f_vs,
            "scorecard": f_scorecard, "Prospect_type": f_prospect,
            "rm_flag": f_rm, "trm10_tier": f_trm, "annual_fee": f_fee,
        })
        blank = {"data": [], "layout": {"title": "No data"}}
        if df.is_empty():
            return blank, blank, blank, blank

        # by vs_band: grouped bars actual vs expected_trm vs expected_xpm
        by_vs = metrics.aggregate_by(df, ["vs_band"]).to_pandas().sort_values("vs_band")
        vs_long = by_vs.melt(
            id_vars="vs_band",
            value_vars=["actual_response_rate", "expected_rr_trm", "expected_rr_xpm"],
            var_name="series", value_name="rate",
        )
        f0 = px.bar(
            vs_long, x="vs_band", y="rate", color="series", barmode="group",
            color_discrete_sequence=[PRIMARY, GOOD, WARN],
            text="rate",
        )
        f0.update_traces(texttemplate="%{text:.2%}", textposition="outside",
                         textfont_size=10)
        f0.update_layout(yaxis_tickformat=".1%", yaxis_title="rate",
                         xaxis_title=None, legend_title="", height=400)

        # by month: actual vs expected_trm
        by_m = metrics.monthly_trend(df).to_pandas().sort_values("campaign_month")
        m_long = by_m.melt(
            id_vars="campaign_month",
            value_vars=["actual_response_rate", "expected_rr_trm"],
            var_name="series", value_name="rate",
        )
        f1 = px.line(m_long, x="campaign_month", y="rate", color="series",
                     markers=True,
                     color_discrete_sequence=[PRIMARY, GOOD])
        f1.update_traces(line=dict(width=2.5),
                         marker=dict(size=7, line=dict(color="#ffffff", width=1.5)))
        f1.update_layout(yaxis_tickformat=".1%", xaxis_title=None,
                         yaxis_title="rate", legend_title="", height=380)

        # by TRM10 tier
        by_trm = metrics.aggregate_by(df, ["trm10_tier"]).to_pandas().sort_values("trm10_tier")
        f2 = px.bar(by_trm, x="trm10_tier", y="actual_response_rate",
                    text="actual_response_rate",
                    color_discrete_sequence=[PRIMARY])
        f2.update_traces(texttemplate="%{text:.2%}", textposition="outside",
                         textfont_size=10)
        f2.update_layout(yaxis_tickformat=".1%", xaxis_title=None,
                         yaxis_title="rate", height=380)

        # by scorecard
        by_sc = metrics.aggregate_by(df, ["scorecard"]).to_pandas().sort_values("scorecard")
        f3 = px.bar(by_sc, x="scorecard", y="actual_vs_expected_trm",
                    text="actual_vs_expected_trm",
                    color_discrete_sequence=[GOOD])
        f3.update_traces(texttemplate="%{text:.2f}", textposition="outside",
                         textfont_size=10)
        f3.update_layout(xaxis_title=None,
                         yaxis_title="actual / expected", height=380)
        return f0, f1, f2, f3

    # ----------------------------------------------------------- data quality
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

    # ----------------------------------------------------------- export
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
