"""Dash callbacks. All read the parquet mart via a cached loader, apply
filters, then delegate aggregation to src.metrics.

Major change vs the first draft:
  - Executive Summary now returns 16 KPI values (Latest + Overall pair per card).
  - Pivot returns a table (with Overall row+column) PLUS a combo chart
    (stacked-bar volume mix + line metric trend).
  - Model Performance has its own filter block and a new by-vs_band chart.
"""
from __future__ import annotations

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
from src.ingest_decile import read_decile_mart

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


@lru_cache(maxsize=1)
def _cached_decile_mart(mart_dir: str, mart_version: float) -> pl.DataFrame:
    return read_decile_mart(mart_dir)


def _decile_mart_version(mart_dir: str) -> float:
    p = Path(mart_dir)
    mtimes = [f.stat().st_mtime for f in p.glob("campaign_month=*/decile.parquet")]
    return max(mtimes) if mtimes else 0.0


def load_decile_mart(cfg: dict) -> pl.DataFrame:
    mart_dir = cfg["paths"]["decile_mart_dir"]
    return _cached_decile_mart(mart_dir, _decile_mart_version(mart_dir))


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


def _apply_month_window(df: pl.DataFrame,
                        multi_months: list | None,
                        month_from: str | None,
                        month_to: str | None,
                        default_n: int) -> pl.DataFrame:
    """Month filter with three layered modes — pick the most specific one.

    Priority (highest first):
      1. Multi-select 'specific months' chosen -> use exactly those (allows
         arbitrary cherry-pick like ['2025-03', '2026-03'] for diff-of-years).
      2. From/To range set on either side -> inclusive range.
      3. Neither -> truncate to most recent `default_n` months when the mart
         has more than that, otherwise show all.

    No mode silently combines with another, so the UI is unambiguous.
    """
    if "campaign_month" not in df.columns:
        return df
    if multi_months:
        return df.filter(pl.col("campaign_month").is_in(list(multi_months)))
    if month_from or month_to:
        if month_from:
            df = df.filter(pl.col("campaign_month") >= month_from)
        if month_to:
            df = df.filter(pl.col("campaign_month") <= month_to)
        return df
    months = df["campaign_month"].unique().sort().to_list()
    if len(months) <= default_n:
        return df
    return df.filter(pl.col("campaign_month").is_in(months[-default_n:]))


def _chart_range_text(df: pl.DataFrame) -> str:
    """Caption used under every chart's H6: 'YYYY-MM → YYYY-MM · N months'."""
    if df.is_empty() or "campaign_month" not in df.columns:
        return ""
    months = df["campaign_month"].unique().sort().to_list()
    if not months:
        return ""
    if len(months) == 1:
        return f"{months[0]} · 1 month"
    return f"{months[0]} → {months[-1]} · {len(months)} months"


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
        Output("kpi-aoe-card", "className"),
        Output("exec-trend-chart", "figure"),
        Output("exec-trend-range", "children"),
        Input("tabs", "active_tab"),
    )
    def _exec(_tab):
        df_full = load_mart(cfg)
        if df_full.is_empty():
            blank = {"data": [], "layout": {"title": "No data"}}
            return ("—",) + ("—",) * 16 + ("kpi-card", blank, "")

        # Latest-month KPIs come from the absolute latest in mart.
        # Overall KPIs and trend chart respect the lookback window so the
        # display doesn't grow unbounded when mart accumulates years.
        lookback = cfg["dashboard"].get("default_lookback_months", 24)
        df = _apply_month_window(df_full, None, None, None, lookback)

        latest_m = _latest_month(df_full)
        latest_df = df_full.filter(pl.col("campaign_month") == latest_m)
        k_latest = metrics.kpi_totals(latest_df)
        k_all = metrics.kpi_totals(df)

        # Separate xpm KPI computation: when reporting the "All months"
        # rate for XPM, divide only by the volume of months that actually
        # carry xpm data — otherwise the rate is artificially diluted by
        # months whose xpm column is null.
        if "expected_responses_xpm" in df.columns:
            df_xpm = df.filter(pl.col("expected_responses_xpm").is_not_null())
        else:
            df_xpm = df.clear()
        k_xpm = metrics.kpi_totals(df_xpm) if not df_xpm.is_empty() else {}

        XPM_KEYS = {"expected_rr_xpm", "actual_vs_expected_xpm"}

        def L(key, fmt):
            return fmt(k_latest.get(key))

        def O(key, fmt):
            # All-months display uses the xpm-restricted denominator for
            # xpm-family metrics; everything else uses the full lookback.
            src = k_xpm if key in XPM_KEYS else k_all
            return fmt(src.get(key))

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

        # Status color for the Actual / Expected (TRM) card. Uses the latest
        # month's ratio: >= 0.95 → good (green), < 0.80 → bad (red), in
        # between → warn (amber). Null → plain.
        aoe_latest = k_latest.get("actual_vs_expected_trm")
        if aoe_latest is None:
            aoe_class = "kpi-card"
        elif aoe_latest >= 0.95:
            aoe_class = "kpi-card good"
        elif aoe_latest < 0.80:
            aoe_class = "kpi-card bad"
        else:
            aoe_class = "kpi-card warn"

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
            aoe_class,
            fig,
            _chart_range_text(df),
        )

    # ----------------------------------------------------------- filter options
    _filter_cols = {
        "f-month-from": "campaign_month",
        "f-month-to":   "campaign_month",
        "f-months":     "campaign_month",
        "f-vs":         "vs_band",
        "f-scorecard":  "scorecard",
        "f-prospect":   "Prospect_type",
        "f-rm":         "rm_flag",
        "f-trm":        "trm10_tier",
        "f-fee":        "annual_fee",
        "f-mailed":     "times_mailed_12mo_cnt",
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
        Output("pivot-table", "style_data_conditional"),
        Output("pivot-combo-chart", "figure"),
        Output("pivot-combo-range", "children"),
        Input("pivot-row-dim", "value"),
        Input("pivot-metric", "value"),
        Input("pivot-suppress", "value"),
        Input("pivot-format", "value"),
        Input("pivot-color-mode", "value"),
        Input("pivot-f-months", "value"),
        Input("pivot-f-month-from", "value"),
        Input("pivot-f-month-to", "value"),
        Input("pivot-f-vs", "value"),
        Input("pivot-f-scorecard", "value"),
        Input("pivot-f-prospect", "value"),
        Input("pivot-f-rm", "value"),
        Input("pivot-f-trm", "value"),
        Input("pivot-f-fee", "value"),
        Input("pivot-f-mailed", "value"),
    )
    def _pivot(row_dim, metric, suppress, fmt, color_mode,
               f_months, f_month_from, f_month_to,
               f_vs, f_scorecard, f_prospect, f_rm, f_trm, f_fee, f_mailed):
        lookback = cfg["dashboard"].get("default_lookback_months", 24)
        df = load_mart(cfg)
        df = _apply_month_window(df, f_months, f_month_from, f_month_to, lookback)
        df = _apply_filters(df, {
            "vs_band": f_vs,
            "scorecard": f_scorecard, "Prospect_type": f_prospect,
            "rm_flag": f_rm, "trm10_tier": f_trm, "annual_fee": f_fee,
            "times_mailed_12mo_cnt": f_mailed,
        })
        empty_fig = {"data": [], "layout": {"title": "No data"}}
        if df.is_empty() or row_dim is None or metric is None:
            return [], [], [], empty_fig, ""

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

        # Pre-compute the wide pivot of the selected metric over numeric (not
        # formatted) values — needed for MoM Δ comparisons below.
        metric_wide_num = (
            cell_masked.to_pandas()
                       .pivot_table(index=row_dim, columns="campaign_month",
                                    values=metric, aggfunc="first")
                       .reset_index()
        )
        metric_wide_num.columns = [str(c) for c in metric_wide_num.columns]
        metric_wide_num = metric_wide_num.rename(columns={row_dim: "Row"})

        # ---- Cell color rules: 'volume' (default) or 'mom' ------------------
        # We never stack both — two background colorings on the same cell would
        # be visually noisy.
        cell_rules: list[dict] = []
        if color_mode == "mom":
            # MoM Δ: compare each cell with the prior month in the same row.
            # Up → green; down → red. Intensity (background alpha) scales with
            # |delta| relative to the largest |delta| in the visible table.
            month_cols = [c for c in metric_wide_num.columns
                          if c not in ("Row", "Overall")]
            deltas: list[tuple[str, str, float]] = []
            for i in range(1, len(month_cols)):
                col = month_cols[i]
                prev = month_cols[i - 1]
                for _, r in metric_wide_num.iterrows():
                    row_val = r["Row"]
                    curr_v, prev_v = r[col], r[prev]
                    if curr_v is None or prev_v is None \
                            or pd.isna(curr_v) or pd.isna(prev_v):
                        continue
                    delta = float(curr_v) - float(prev_v)
                    if delta == 0:
                        continue
                    deltas.append((col, str(row_val), delta))
            if deltas:
                max_abs = max(abs(d[2]) for d in deltas)
                # alpha range: 0.18 (barely visible) → 0.70 (saturated)
                for col, row_val, delta in deltas:
                    intensity = min(1.0, abs(delta) / max_abs) if max_abs > 0 else 0
                    alpha = 0.18 + 0.52 * intensity
                    if delta > 0:
                        bg = f"rgba(46, 122, 82, {alpha:.2f})"   # GOOD green
                    else:
                        bg = f"rgba(179, 67, 74, {alpha:.2f})"   # BAD red
                    rv = row_val.replace('"', '\\"')
                    cell_rules.append({
                        "if": {"column_id": col,
                               "filter_query": f'{{Row}} = "{rv}"'},
                        "backgroundColor": bg,
                        "color": "#0e2238",
                    })
        else:
            # Volume bars: gradient width = cell volume / column total volume.
            vol_cell = (
                df.group_by([row_dim, "campaign_month"])
                  .agg(pl.col("volume").sum().alias("volume"))
                  .to_pandas()
            )
            vol_wide = vol_cell.pivot_table(
                index=row_dim, columns="campaign_month",
                values="volume", aggfunc="first",
            )
            vol_wide.columns = [str(c) for c in vol_wide.columns]
            row_totals = vol_wide.sum(axis=1)
            grand_total = row_totals.sum()
            vol_wide["Overall"] = row_totals if (grand_total and grand_total > 0) else 0.0
            for col in vol_wide.columns:
                col_total = vol_wide[col].sum()
                if not col_total or col_total <= 0:
                    continue
                for row_val, vol in vol_wide[col].items():
                    if vol is None or pd.isna(vol) or vol <= 0:
                        continue
                    pct = max(2, min(100, (vol / col_total) * 100))
                    rv = str(row_val).replace('"', '\\"')
                    cell_rules.append({
                        "if": {"column_id": col,
                               "filter_query": f'{{Row}} = "{rv}"'},
                        "background":
                            f"linear-gradient(90deg, rgba(26,77,140,0.10) {pct:.1f}%, "
                            f"transparent {pct:.1f}%)",
                    })

        # Overall row goes LAST so it overrides any per-cell coloring.
        style_rules = cell_rules + [
            {"if": {"filter_query": "{Row} = 'Overall'"},
             "backgroundColor": "#e8eff8", "fontWeight": "700",
             "color": "#0e2238"},
        ]

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
        return data, columns, style_rules, fig, _chart_range_text(df)

    # ----------------------------------------------------------- model perf
    @app.callback(
        Output("model-by-vsband-trm", "figure"),
        Output("model-vs-trm-range", "children"),
        Output("model-by-vsband-xpm", "figure"),
        Output("model-vs-xpm-range", "children"),
        Output("model-arr-vs-exp", "figure"),
        Output("model-monthly-range", "children"),
        Output("model-by-trm", "figure"),
        Output("model-trm-range", "children"),
        Output("model-by-scorecard", "figure"),
        Output("model-sc-range", "children"),
        Input("model-f-months", "value"),
        Input("model-f-month-from", "value"),
        Input("model-f-month-to", "value"),
        Input("model-f-vs", "value"),
        Input("model-f-scorecard", "value"),
        Input("model-f-prospect", "value"),
        Input("model-f-rm", "value"),
        Input("model-f-trm", "value"),
        Input("model-f-fee", "value"),
        Input("model-f-mailed", "value"),
    )
    def _model(f_months, f_month_from, f_month_to,
               f_vs, f_scorecard, f_prospect, f_rm, f_trm, f_fee, f_mailed):
        lookback = cfg["dashboard"].get("default_lookback_months", 24)
        df = load_mart(cfg)
        df = _apply_month_window(df, f_months, f_month_from, f_month_to, lookback)
        df = _apply_filters(df, {
            "vs_band": f_vs,
            "scorecard": f_scorecard, "Prospect_type": f_prospect,
            "rm_flag": f_rm, "trm10_tier": f_trm, "annual_fee": f_fee,
            "times_mailed_12mo_cnt": f_mailed,
        })
        blank = {"data": [], "layout": {"title": "No data"}}
        if df.is_empty():
            return (blank, "", blank, "", blank, "", blank, "", blank, "")

        full_range = _chart_range_text(df)

        # Chart 1: by vs_band — Actual vs Expected (TRM). Uses all months.
        by_vs_trm = metrics.aggregate_by(df, ["vs_band"]).to_pandas().sort_values("vs_band")
        vs_long_trm = by_vs_trm.melt(
            id_vars="vs_band",
            value_vars=["actual_response_rate", "expected_rr_trm"],
            var_name="series", value_name="rate",
        )
        f_trm = px.bar(
            vs_long_trm, x="vs_band", y="rate", color="series", barmode="group",
            color_discrete_sequence=[PRIMARY, GOOD], text="rate",
        )
        f_trm.update_traces(texttemplate="%{text:.2%}", textposition="outside",
                            textfont_size=10)
        f_trm.update_layout(yaxis_tickformat=".1%", yaxis_title="rate",
                            xaxis_title=None, legend_title="", height=400)

        # Chart 2: by vs_band — Actual vs Expected (XPM). XPM is null for
        # months where SAS did not source EXP_RESPONSE_SCORE; restrict to
        # rows where the column is non-null so the comparison is fair.
        df_xpm = df.filter(pl.col("expected_responses_xpm").is_not_null()) \
            if "expected_responses_xpm" in df.columns else df.clear()
        if df_xpm.is_empty():
            f_xpm = blank
            xpm_range = "No months with XPM available"
        else:
            by_vs_xpm = metrics.aggregate_by(df_xpm, ["vs_band"]).to_pandas().sort_values("vs_band")
            vs_long_xpm = by_vs_xpm.melt(
                id_vars="vs_band",
                value_vars=["actual_response_rate", "expected_rr_xpm"],
                var_name="series", value_name="rate",
            )
            f_xpm = px.bar(
                vs_long_xpm, x="vs_band", y="rate", color="series", barmode="group",
                color_discrete_sequence=[PRIMARY, WARN], text="rate",
            )
            f_xpm.update_traces(texttemplate="%{text:.2%}", textposition="outside",
                                textfont_size=10)
            f_xpm.update_layout(yaxis_tickformat=".1%", yaxis_title="rate",
                                xaxis_title=None, legend_title="", height=400)
            xpm_range = "XPM months only · " + _chart_range_text(df_xpm)

        # Chart 3: by month — actual vs expected_trm vs expected_xpm
        # expected_rr_xpm is nulled by aggregate_by for months without xpm
        # data, so the line is automatically discontinuous (no false zeros).
        by_m = metrics.monthly_trend(df).to_pandas().sort_values("campaign_month")
        m_long = by_m.melt(
            id_vars="campaign_month",
            value_vars=["actual_response_rate", "expected_rr_trm", "expected_rr_xpm"],
            var_name="series", value_name="rate",
        )
        # Drop rows where the rate is null so xpm gaps render as breaks.
        m_long = m_long.dropna(subset=["rate"])
        f1 = px.line(m_long, x="campaign_month", y="rate", color="series",
                     markers=True,
                     color_discrete_sequence=[PRIMARY, GOOD, WARN])
        f1.update_traces(line=dict(width=2.5),
                         marker=dict(size=7, line=dict(color="#ffffff", width=1.5)))
        f1.update_layout(yaxis_tickformat=".1%", xaxis_title=None,
                         yaxis_title="rate", legend_title="", height=380)

        # Chart 4: by TRM10 tier — actual response rate
        by_trm = metrics.aggregate_by(df, ["trm10_tier"]).to_pandas().sort_values("trm10_tier")
        f2 = px.bar(by_trm, x="trm10_tier", y="actual_response_rate",
                    text="actual_response_rate",
                    color_discrete_sequence=[PRIMARY])
        f2.update_traces(texttemplate="%{text:.2%}", textposition="outside",
                         textfont_size=10)
        f2.update_layout(yaxis_tickformat=".1%", xaxis_title=None,
                         yaxis_title="rate", height=380)

        # Chart 5: by scorecard — actual / expected ratio
        by_sc = metrics.aggregate_by(df, ["scorecard"]).to_pandas().sort_values("scorecard")
        f3 = px.bar(by_sc, x="scorecard", y="actual_vs_expected_trm",
                    text="actual_vs_expected_trm",
                    color_discrete_sequence=[GOOD])
        f3.update_traces(texttemplate="%{text:.2f}", textposition="outside",
                         textfont_size=10)
        f3.update_layout(xaxis_title=None,
                         yaxis_title="actual / expected", height=380)

        return (f_trm, full_range, f_xpm, xpm_range,
                f1, full_range, f2, full_range, f3, full_range)

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

    # ----------------------------------------------------------- rank order
    @app.callback(
        Output("rank-f-month", "options"),
        Output("rank-f-month", "value"),
        Output("rank-f-scorecard", "options"),
        Output("rank-f-scorecard", "value"),
        Input("tabs", "active_tab"),
    )
    def _rank_filter_options(_tab):
        d = load_decile_mart(cfg)
        if d.is_empty():
            return [], None, [], None
        months = d["campaign_month"].unique().sort().to_list()
        scs = d["scorecard"].drop_nulls().unique().sort().to_list()
        return (
            [{"label": m, "value": m} for m in months], months[-1] if months else None,
            [{"label": "All scorecards", "value": "__all__"}]
                + [{"label": f"Scorecard {s}", "value": int(s)} for s in scs],
            "__all__",
        )

    @app.callback(
        Output("rank-ks", "children"),
        Output("rank-top-lift", "children"),
        Output("rank-volume", "children"),
        Output("rank-resp", "children"),
        Output("rank-capture-chart", "figure"),
        Output("rank-capture-range", "children"),
        Output("rank-rr-chart", "figure"),
        Output("rank-rr-range", "children"),
        Output("rank-ks-chart", "figure"),
        Output("rank-ks-range", "children"),
        Output("rank-table", "data"),
        Output("rank-table", "columns"),
        Output("rank-table-range", "children"),
        Input("rank-f-month", "value"),
        Input("rank-f-scorecard", "value"),
    )
    def _rank(month, scorecard):
        d = load_decile_mart(cfg)
        blank = {"data": [], "layout": {"title": "No decile data"}}
        if d.is_empty():
            empty_msg = ("Decile mart is empty. Run SAS for at least one month "
                         "after the new %rollup_decile macro is installed, "
                         "then re-run the monthly refresh.")
            return ("—", "—", "—", "—",
                    blank, empty_msg, blank, empty_msg,
                    blank, empty_msg, [], [], empty_msg)

        sc_filter = None if (scorecard in (None, "__all__")) else int(scorecard)
        target_month = month or d["campaign_month"].max()

        summary = metrics.decile_summary(d, scorecard=sc_filter,
                                         campaign_month=target_month)
        ks = metrics.ks_value(d, scorecard=sc_filter,
                              campaign_month=target_month)

        # ---- KPI cards
        if summary.is_empty():
            ks_str = top_lift_str = vol_str = resp_str = "—"
        else:
            ks_str = _fmt_pct(ks) if ks is not None else "—"
            top_lift_str = _fmt_ratio(summary["lift"][0]) if summary["lift"][0] is not None else "—"
            vol_str = _fmt_count(int(summary["volume"].sum()))
            resp_str = _fmt_count(int(summary["responders"].sum()))

        sc_caption = "all scorecards" if sc_filter is None else f"scorecard {sc_filter}"
        range_text = f"{target_month} · {sc_caption}"

        # ---- Cumulative capture curve (+ diagonal reference)
        if summary.is_empty():
            fig_cap = blank
        else:
            xs = [0.0] + summary["cum_volume_pct"].to_list()
            ys = [0.0] + summary["cum_capture"].to_list()
            fig_cap = go.Figure()
            fig_cap.add_scatter(x=[0, 1], y=[0, 1], mode="lines",
                                line=dict(color="#a9b8c8", width=1.5, dash="dash"),
                                name="random", hoverinfo="skip")
            fig_cap.add_scatter(x=xs, y=ys, mode="lines+markers",
                                fill="tozeroy", fillcolor="rgba(26,77,140,0.10)",
                                line=dict(color=PRIMARY, width=3),
                                marker=dict(size=7, line=dict(color="#ffffff", width=1.5)),
                                name="capture")
            fig_cap.update_layout(
                xaxis=dict(title="Cumulative volume %", tickformat=".0%", range=[0, 1.02]),
                yaxis=dict(title="Cumulative capture %", tickformat=".0%", range=[0, 1.02]),
                height=400, legend=dict(orientation="h", y=1.05, x=1, xanchor="right"),
            )

        # ---- Response rate by decile (bar)
        if summary.is_empty():
            fig_rr = blank
        else:
            pdf = summary.to_pandas()
            fig_rr = px.bar(pdf, x="total_decile", y="response_rate",
                            text="response_rate",
                            color_discrete_sequence=[PRIMARY])
            fig_rr.update_traces(texttemplate="%{text:.2%}", textposition="outside",
                                 textfont_size=10)
            fig_rr.update_layout(yaxis_tickformat=".1%",
                                 xaxis_title="Decile (1 = highest score)",
                                 yaxis_title="response rate",
                                 height=380)

        # ---- KS over time
        ks_df = metrics.ks_by_month(d, scorecard=sc_filter).to_pandas()
        ks_range = (f"{ks_df['campaign_month'].min()} → "
                    f"{ks_df['campaign_month'].max()} · "
                    f"{len(ks_df)} months · {sc_caption}") if not ks_df.empty else ""
        if ks_df.empty:
            fig_ks = blank
        else:
            fig_ks = px.line(ks_df, x="campaign_month", y="ks", markers=True,
                             color_discrete_sequence=[PRIMARY])
            fig_ks.update_traces(line=dict(width=2.5),
                                 marker=dict(size=8, line=dict(color="#ffffff", width=1.5)))
            fig_ks.update_layout(yaxis_tickformat=".1%",
                                 xaxis_title=None, yaxis_title="KS",
                                 height=360)

        # ---- Decile detail table
        if summary.is_empty():
            table_data, table_cols = [], []
        else:
            display = summary.select([
                "total_decile", "volume", "responders", "Boards",
                "response_rate", "cum_capture", "cum_volume_pct", "lift",
            ]).to_pandas()
            for c in ("response_rate", "cum_capture", "cum_volume_pct"):
                display[c] = display[c].apply(_fmt_pct)
            display["lift"] = display["lift"].apply(_fmt_ratio)
            display["volume"] = display["volume"].apply(_fmt_count)
            display["responders"] = display["responders"].apply(_fmt_count)
            display["Boards"] = display["Boards"].apply(_fmt_count)
            display.rename(columns={
                "total_decile": "Decile",
                "response_rate": "Response rate",
                "cum_capture": "Cum capture",
                "cum_volume_pct": "Cum volume",
            }, inplace=True)
            table_data = display.to_dict("records")
            table_cols = [{"name": c, "id": c} for c in display.columns]

        return (ks_str, top_lift_str, vol_str, resp_str,
                fig_cap, range_text, fig_rr, range_text,
                fig_ks, ks_range, table_data, table_cols, range_text)

    # ----------------------------------------------------------- export
    @app.callback(
        Output("export-download", "data"),
        Input("btn-export-csv", "n_clicks"),
        Input("btn-export-xlsx", "n_clicks"),
        State("export-f-months", "value"),
        State("export-f-month-from", "value"),
        State("export-f-month-to", "value"),
        State("export-f-vs", "value"),
        State("export-f-scorecard", "value"),
        State("export-f-prospect", "value"),
        State("export-f-rm", "value"),
        State("export-f-trm", "value"),
        State("export-f-fee", "value"),
        State("export-f-mailed", "value"),
        prevent_initial_call=True,
    )
    def _export(n_csv, n_xlsx,
                f_months, f_month_from, f_month_to,
                f_vs, f_scorecard, f_prospect, f_rm, f_trm, f_fee, f_mailed):
        from dash import ctx
        if not ctx.triggered_id:
            return no_update
        # Export respects the exact user range; no default lookback truncation.
        # Same priority as the dashboard: multi-select wins over From/To.
        df = load_mart(cfg)
        if f_months:
            df = df.filter(pl.col("campaign_month").is_in(list(f_months)))
        elif f_month_from or f_month_to:
            if f_month_from:
                df = df.filter(pl.col("campaign_month") >= f_month_from)
            if f_month_to:
                df = df.filter(pl.col("campaign_month") <= f_month_to)
        df = _apply_filters(df, {
            "vs_band": f_vs,
            "scorecard": f_scorecard, "Prospect_type": f_prospect,
            "rm_flag": f_rm, "trm10_tier": f_trm, "annual_fee": f_fee,
            "times_mailed_12mo_cnt": f_mailed,
        }).to_pandas()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if ctx.triggered_id == "btn-export-csv":
            return dcc.send_data_frame(df.to_csv,
                                       f"rate_response_{ts}.csv",
                                       index=False)
        # dcc.send_data_frame handles BytesIO + MIME under the hood; works
        # reliably whenever openpyxl is installed.
        return dcc.send_data_frame(df.to_excel,
                                   f"rate_response_{ts}.xlsx",
                                   sheet_name="rollup",
                                   index=False)
