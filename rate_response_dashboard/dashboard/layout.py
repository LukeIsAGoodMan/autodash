"""Static layout for the Dash app. Class-based styling — all visual rules in
assets/custom.css. This module only declares structure and IDs.

Macro structure:
  hero            : title + subtitle + tags + verdict badge (gradient bg)
  tabs (underline): Executive | Pivot | Model | DQ | Export
    tab content   : sticky controls bar at top (where applicable) + panels
"""
from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html


# ---------------------------------------------------------------- helpers
def kpi_card_dual(title: str, latest_id: str, overall_id: str,
                  variant: str = "headline",
                  card_id: str | None = None) -> html.Div:
    """KPI card with the latest-month value as the big number and an overall
    subtitle below. `variant` controls the top-border color: 'headline' (blue),
    'good', 'bad', 'warn', or '' (no accent). If `card_id` is provided, the
    outer div gets an id so a callback can swap className dynamically (used
    for status-driven coloring like actual/expected)."""
    classes = "kpi-card"
    if variant:
        classes += f" {variant}"
    kwargs = {"className": classes}
    if card_id:
        kwargs["id"] = card_id
    return html.Div([
        html.Div(title, className="k-label"),
        html.Div(id=latest_id, className="k-value"),
        html.Div([
            html.Span("All months", className="k-sub-label"),
            html.Span(id=overall_id, className="k-sub-value"),
        ], className="k-sub"),
    ], **kwargs)


def kpi_card_single(title: str, kpi_id: str, variant: str = "") -> html.Div:
    classes = "kpi-card"
    if variant:
        classes += f" {variant}"
    return html.Div([
        html.Div(title, className="k-label"),
        html.Div(id=kpi_id, className="k-value"),
    ], className=classes)


def section_header(eyebrow_text: str, sub: str | None = None,
                   month_chip_id: str | None = None) -> html.Div:
    eyebrow_children = [eyebrow_text]
    if month_chip_id:
        # Insert a styled month chip after the text, e.g. "Latest month [2026-05] at a glance"
        eyebrow_children = [
            "Latest month ",
            html.Span(id=month_chip_id, className="month-chip"),
            " at a glance",
        ]
    children = [
        html.Div(eyebrow_children, className="eyebrow"),
    ]
    if sub:
        children.append(html.Div(sub, className="section-sub"))
    return html.Div(children, className="section-header")


def hero() -> html.Div:
    return html.Div([
        html.Div([
            html.H1("Rate Response Dashboard"),
            html.Div(
                "Monthly campaign volume, response and board performance",
                className="hero-sub",
            ),
            html.Div([
                html.Span([
                    html.Span("Latest month ", className="tag-label"),
                    html.Span(id="hero-latest", children="—"),
                ], className="tag"),
                html.Span([
                    html.Span("Months in mart ", className="tag-label"),
                    html.Span(id="hero-months", children="—"),
                ], className="tag"),
                html.Span([
                    html.Span("Last refresh ", className="tag-label"),
                    html.Span(id="hero-refresh", children="—"),
                ], className="tag"),
            ], className="tags"),
        ]),
        html.Div([
            html.Span(className="dot"),
            html.Span(id="hero-verdict-text", children="data fresh"),
        ], className="verdict"),
    ], className="hero")


# ---------------------------------------------------------------- tab builders
def tab_executive() -> dbc.Tab:
    row1 = dbc.Row([
        dbc.Col(kpi_card_dual("Total volume",       "kpi-vol-latest",   "kpi-vol-overall",   ""), md=3),
        dbc.Col(kpi_card_dual("Responders",         "kpi-resp-latest",  "kpi-resp-overall",  ""), md=3),
        dbc.Col(kpi_card_dual("Boards",             "kpi-boards-latest","kpi-boards-overall",""), md=3),
        dbc.Col(kpi_card_dual("Actual board rate",  "kpi-abr-latest",   "kpi-abr-overall", "headline"), md=3),
    ], className="g-3")
    row2 = dbc.Row([
        dbc.Col(kpi_card_dual("Actual response rate",     "kpi-arr-latest", "kpi-arr-overall", ""), md=3),
        dbc.Col(kpi_card_dual("Expected RR (TRM)",        "kpi-trm-latest", "kpi-trm-overall", ""), md=3),
        dbc.Col(kpi_card_dual("Expected RR (XPM)",        "kpi-xpm-latest", "kpi-xpm-overall", ""), md=3),
        dbc.Col(kpi_card_dual("Actual / Expected (TRM)",  "kpi-aoe-latest", "kpi-aoe-overall",
                              variant="", card_id="kpi-aoe-card"), md=3),
    ], className="g-3 mt-3")

    trend = dbc.Card(dbc.CardBody([
        html.H6("Monthly trend"),
        html.Div(id="exec-trend-range", className="chart-meta"),
        dcc.Graph(id="exec-trend-chart", config={"displaylogo": False}),
    ]), className="mt-4")

    return dbc.Tab(
        label="Executive Summary",
        tab_id="tab-exec",
        children=html.Div([
            section_header("", "Big number is the latest month; smaller line is the all-month total.",
                           month_chip_id="exec-latest-month-badge"),
            row1, row2,
            section_header("Monthly trend"),
            trend,
        ], className="tab-content-area"),
    )


def tab_pivot(cfg: dict) -> dbc.Tab:
    dims = cfg["catalog"]["dimensions"]
    metrics = cfg["catalog"]["metrics"]

    controls_and_filters = html.Div([
        dbc.Row([
            dbc.Col([
                html.Label("Row dimension"),
                dcc.Dropdown(
                    id="pivot-row-dim",
                    options=[{"label": d, "value": d} for d in dims],
                    value=cfg["dashboard"]["default_row_dim"],
                    clearable=False,
                ),
            ], md=3),
            dbc.Col([
                html.Label("Metric (cells + line)"),
                dcc.Dropdown(
                    id="pivot-metric",
                    options=[{"label": m, "value": m} for m in metrics],
                    value=cfg["dashboard"]["default_metric"],
                    clearable=False,
                ),
            ], md=3),
            dbc.Col([
                html.Label("Suppress cells with volume <"),
                dcc.Input(id="pivot-suppress", type="number",
                          value=cfg["dashboard"]["small_cell_threshold"],
                          min=0, step=10),
            ], md=2),
            dbc.Col([
                html.Label("Cell format"),
                dcc.RadioItems(
                    id="pivot-format",
                    options=[{"label": " Percent ", "value": "pct"},
                             {"label": " Number ", "value": "num"}],
                    value="pct",
                    inline=True,
                ),
            ], md=2),
            dbc.Col([
                html.Label("Color mode"),
                dcc.RadioItems(
                    id="pivot-color-mode",
                    options=[{"label": " Volume bars ", "value": "volume"},
                             {"label": " MoM Δ ", "value": "mom"}],
                    value="volume",
                    inline=True,
                ),
            ], md=2),
        ], className="g-3"),
        html.Hr(),
        _filter_inner(cfg, prefix="pivot"),
    ], className="controls-bar")

    table = dash_table.DataTable(
        id="pivot-table",
        page_size=50,
        style_table={"overflowX": "auto"},
        style_cell={"padding": "8px", "fontFamily": "Segoe UI, sans-serif",
                    "fontSize": "12.5px"},
        # Static rules live here; the callback emits a fuller list that
        # includes per-cell volume gradients in addition to these.
        style_data_conditional=[
            {"if": {"filter_query": "{Row} = 'Overall'"},
             "backgroundColor": "#e8eff8", "fontWeight": "700"},
        ],
    )

    combo = dbc.Card(dbc.CardBody([
        html.H6("Volume mix (stacked bars) + metric trend (line)"),
        html.Div(id="pivot-combo-range", className="chart-meta"),
        dcc.Graph(id="pivot-combo-chart", config={"displaylogo": False}),
    ]), className="mt-3")

    return dbc.Tab(
        label="Pivot View",
        tab_id="tab-pivot",
        children=html.Div([
            controls_and_filters,
            section_header("Pivot",
                           "Last row 'Overall' aggregates across the selected row-dim "
                           "values; last column 'Overall' aggregates across months."),
            html.Div(table, className="mt-2"),
            combo,
        ], className="tab-content-area"),
    )


def tab_model(cfg: dict) -> dbc.Tab:
    filters = html.Div(_filter_inner(cfg, prefix="model"),
                       className="controls-bar")

    # Split the old combined chart into two — TRM (all months) and XPM
    # (months with EXP_RESPONSE_SCORE only). Without this split, the XPM
    # bars look near-zero because most months contribute volume but no xpm.
    by_vsband_trm = dbc.Card(dbc.CardBody([
        html.H6("Actual RR vs Expected RR (TRM) by vs_band"),
        html.Div(id="model-vs-trm-range", className="chart-meta"),
        dcc.Graph(id="model-by-vsband-trm", config={"displaylogo": False}),
    ]), className="mb-3")

    by_vsband_xpm = dbc.Card(dbc.CardBody([
        html.H6("Actual RR vs Expected RR (XPM) by vs_band"),
        html.Div(id="model-vs-xpm-range", className="chart-meta"),
        dcc.Graph(id="model-by-vsband-xpm", config={"displaylogo": False}),
    ]), className="mb-3")

    monthly = dbc.Card(dbc.CardBody([
        html.H6("Actual RR vs Expected RR by month"),
        html.Div(id="model-monthly-range", className="chart-meta"),
        dcc.Graph(id="model-arr-vs-exp", config={"displaylogo": False}),
    ]), className="mb-3")

    by_trm = dbc.Card(dbc.CardBody([
        html.H6("Actual RR by TRM10 tier"),
        html.Div(id="model-trm-range", className="chart-meta"),
        dcc.Graph(id="model-by-trm", config={"displaylogo": False}),
    ]))

    by_sc = dbc.Card(dbc.CardBody([
        html.H6("Actual / Expected by scorecard"),
        html.Div(id="model-sc-range", className="chart-meta"),
        dcc.Graph(id="model-by-scorecard", config={"displaylogo": False}),
    ]))

    return dbc.Tab(
        label="Model Performance",
        tab_id="tab-model",
        children=html.Div([
            filters,
            section_header("Calibration"),
            by_vsband_trm,
            by_vsband_xpm,
            monthly,
            dbc.Row([dbc.Col(by_trm, md=6), dbc.Col(by_sc, md=6)], className="g-3"),
        ], className="tab-content-area"),
    )


def tab_rankorder() -> dbc.Tab:
    """Decile-grain rank-order analytics: KS, capture curve, decile table."""
    controls = html.Div([
        dbc.Row([
            dbc.Col([
                html.Label("Campaign month"),
                dcc.Dropdown(id="rank-f-month", clearable=False,
                             placeholder="(latest)"),
            ], md=3),
            dbc.Col([
                html.Label("Scorecard"),
                dcc.Dropdown(id="rank-f-scorecard", clearable=False,
                             placeholder="(all)"),
            ], md=3),
        ], className="g-3"),
    ], className="controls-bar")

    kpi_row = dbc.Row([
        dbc.Col(kpi_card_single("KS",                "rank-ks",     variant="headline"), md=3),
        dbc.Col(kpi_card_single("Top decile lift",   "rank-top-lift"),                   md=3),
        dbc.Col(kpi_card_single("Total volume",      "rank-volume"),                     md=3),
        dbc.Col(kpi_card_single("Total responders",  "rank-resp"),                       md=3),
    ], className="g-3")

    capture = dbc.Card(dbc.CardBody([
        html.H6("Cumulative capture vs cumulative volume"),
        html.Div(id="rank-capture-range", className="chart-meta"),
        dcc.Graph(id="rank-capture-chart", config={"displaylogo": False}),
    ]), className="mb-3")

    rr_decile = dbc.Card(dbc.CardBody([
        html.H6("Response rate by decile"),
        html.Div(id="rank-rr-range", className="chart-meta"),
        dcc.Graph(id="rank-rr-chart", config={"displaylogo": False}),
    ]), className="mb-3")

    ks_trend = dbc.Card(dbc.CardBody([
        html.H6("KS over time"),
        html.Div(id="rank-ks-range", className="chart-meta"),
        dcc.Graph(id="rank-ks-chart", config={"displaylogo": False}),
    ]), className="mb-3")

    decile_table = dbc.Card(dbc.CardBody([
        html.H6("Decile detail"),
        html.Div(id="rank-table-range", className="chart-meta"),
        dash_table.DataTable(
            id="rank-table",
            page_size=12,
            style_table={"overflowX": "auto"},
            style_cell={"padding": "8px", "fontFamily": "Segoe UI, sans-serif",
                        "fontSize": "12.5px"},
            style_data_conditional=[
                {"if": {"row_index": "odd"}, "backgroundColor": "#fafbfd"},
            ],
        ),
    ]))

    return dbc.Tab(
        label="Rank Order",
        tab_id="tab-rankorder",
        children=html.Div([
            controls,
            section_header("Headline"),
            kpi_row,
            section_header("Capture & calibration"),
            capture,
            rr_decile,
            section_header("Stability"),
            ks_trend,
            section_header("Detail"),
            decile_table,
        ], className="tab-content-area"),
    )


def tab_dq() -> dbc.Tab:
    table = dash_table.DataTable(
        id="dq-table",
        page_size=24,
        style_table={"overflowX": "auto"},
        style_cell={"padding": "8px", "fontFamily": "Segoe UI, sans-serif",
                    "fontSize": "12.5px"},
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#fafbfd"},
            {"if": {"filter_query": "{has_xpm} = false", "column_id": "has_xpm"},
             "backgroundColor": "#fdecea", "color": "#922b21", "fontWeight": "600"},
        ],
    )
    return dbc.Tab(
        label="Data Quality",
        tab_id="tab-dq",
        children=html.Div([
            section_header("Snapshot"),
            dbc.Row([
                dbc.Col(kpi_card_single("Latest month in mart", "dq-latest"), md=4),
                dbc.Col(kpi_card_single("Last refresh timestamp", "dq-last-refresh"), md=4),
                dbc.Col(kpi_card_single("Partitions present", "dq-partition-count"), md=4),
            ], className="g-3"),
            section_header("Per-month summary",
                           "has_xpm=false means expected_responses_xpm is null "
                           "for that month (SAS pipeline did not source EXP_RESPONSE_SCORE)."),
            table,
        ], className="tab-content-area"),
    )


def tab_export(cfg: dict) -> dbc.Tab:
    return dbc.Tab(
        label="Export",
        tab_id="tab-export",
        children=html.Div([
            section_header("Download filtered view",
                           "Choose filters, then download the aggregated rollup."),
            html.Div(_filter_inner(cfg, prefix="export"), className="controls-bar"),
            html.Div([
                dbc.Button("Download CSV", id="btn-export-csv",
                           color="primary", className="me-2"),
                dbc.Button("Download Excel", id="btn-export-xlsx",
                           color="secondary"),
            ], className="mt-3"),
            dcc.Download(id="export-download"),
            html.Hr(className="mt-4"),
            section_header("Metric definitions"),
            html.Ul([
                html.Li("actual_response_rate = sum(responders) / sum(volume)"),
                html.Li("actual_board_rate = sum(Boards) / sum(volume)"),
                html.Li("expected_rr_trm = sum(expected_responses) / sum(volume)"),
                html.Li("expected_rr_xpm = sum(expected_responses_xpm) / sum(volume)"),
                html.Li("actual_vs_expected_* = actual_response_rate / expected_rr_*"),
                html.Li("All rates are recomputed from sums. Rates are never averaged."),
            ], className="small"),
        ], className="tab-content-area"),
    )


# ------------------------------------------- shared filter row(s), no wrapper
def _filter_inner(cfg: dict, prefix: str) -> html.Div:
    """The filter dropdowns. Uniform widths (4 per row, md=3 each).
    The 'Campaign months' cell contains two From/To single-selects for
    picking an explicit time range, e.g. 2025-12 → 2026-03."""
    months_cell = dbc.Col([
        html.Label("Campaign months"),
        dbc.Row([
            dbc.Col(dcc.Dropdown(id=f"{prefix}-f-month-from",
                                 placeholder="from", clearable=True), width=6),
            dbc.Col(dcc.Dropdown(id=f"{prefix}-f-month-to",
                                 placeholder="to", clearable=True), width=6),
        ], className="g-1"),
        dcc.Dropdown(id=f"{prefix}-f-months", multi=True,
                     placeholder="or pick specific months (overrides range)",
                     className="mt-1"),
    ], md=3)

    def col(label: str, suffix: str):
        return dbc.Col([
            html.Label(label),
            dcc.Dropdown(id=f"{prefix}-{suffix}", multi=True, placeholder="all"),
        ], md=3)

    return html.Div([
        dbc.Row([
            months_cell,
            col("vs_band",       "f-vs"),
            col("scorecard",     "f-scorecard"),
            col("Prospect_type", "f-prospect"),
        ], className="g-2"),
        dbc.Row([
            col("rm_flag",                "f-rm"),
            col("trm10_tier",             "f-trm"),
            col("annual_fee",             "f-fee"),
            col("times_mailed_12mo_cnt",  "f-mailed"),
        ], className="g-2 mt-2"),
    ])


# Kept for backward compatibility with callbacks that import filter_block.
def filter_block(cfg: dict, prefix: str) -> html.Div:
    return _filter_inner(cfg, prefix)


# ---------------------------------------------------------------- root
def build_layout(cfg: dict) -> html.Div:
    return html.Div([
        dcc.Store(id="cfg-store", data=cfg),
        hero(),
        dbc.Tabs(
            [
                tab_executive(),
                tab_pivot(cfg),
                tab_model(cfg),
                tab_rankorder(),
                tab_dq(),
                tab_export(cfg),
            ],
            id="tabs",
            active_tab="tab-exec",
        ),
    ])
