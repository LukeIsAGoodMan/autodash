"""Static layout for the Dash app. All interactivity lives in callbacks.py.

Visual hierarchy:
  - Header bar (dark slate)        : product title only
  - Subtitle strip (lighter slate) : latest month + last refresh + freshness dot
  - Five tabs, each card-based

KPI cards in Executive show two values per card:
  the latest-month value (big), and overall (small subtitle).
Pivot adds an Overall row/column and a stacked-bar + line combo chart.
Model Performance has its own filter block and a per-vs_band comparison chart.
"""
from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

# ---------------------------------------------------------------- style tokens
PRIMARY = "#1b4f72"        # deep blue for headers / accents
ACCENT = "#2980b9"         # secondary accent
SUBTLE_BG = "#f5f7fa"      # page background tint
CARD_SHADOW = "0 2px 8px rgba(0,0,0,0.06)"

HEADER_STYLE = {
    "background": f"linear-gradient(90deg, {PRIMARY} 0%, {ACCENT} 100%)",
    "color": "#ffffff",
    "padding": "18px 28px",
    "marginBottom": "0",
    "boxShadow": CARD_SHADOW,
}
SUBTITLE_STYLE = {
    "backgroundColor": "#ecf0f1",
    "color": "#2c3e50",
    "padding": "10px 28px",
    "fontSize": "13px",
    "marginBottom": "18px",
    "borderBottom": "1px solid #d0d7de",
}
KPI_CARD_STYLE = {
    "minHeight": "126px",
    "borderRadius": "10px",
    "border": "none",
    "boxShadow": CARD_SHADOW,
}
SECTION_HEADER_STYLE = {
    "color": PRIMARY,
    "borderLeft": f"4px solid {ACCENT}",
    "paddingLeft": "10px",
    "marginTop": "6px",
    "marginBottom": "12px",
    "fontWeight": "600",
}
CONTENT_STYLE = {"padding": "0 28px 28px 28px", "backgroundColor": SUBTLE_BG, "minHeight": "100vh"}
TAB_LABEL_STYLE = {"fontWeight": "500"}


# ---------------------------------------------------------------- helpers
def kpi_card_dual(title: str, latest_id: str, overall_id: str) -> dbc.Card:
    """KPI card showing the latest-month value (big) and overall (subtitle)."""
    return dbc.Card(
        dbc.CardBody([
            html.Div(title, className="text-muted small text-uppercase fw-bold"),
            html.Div(id=latest_id, className="h3 mb-1", style={"color": PRIMARY}),
            html.Div([
                html.Small("All months: ", className="text-muted"),
                html.Small(id=overall_id, className="text-muted fw-semibold"),
            ]),
        ]),
        style=KPI_CARD_STYLE,
    )


def kpi_card(title: str, kpi_id: str, sub_id: str | None = None) -> dbc.Card:
    body = [
        html.Div(title, className="text-muted small text-uppercase fw-bold"),
        html.Div(id=kpi_id, className="h3 mb-0", style={"color": PRIMARY}),
    ]
    if sub_id:
        body.append(html.Div(id=sub_id, className="text-muted small"))
    return dbc.Card(dbc.CardBody(body), style=KPI_CARD_STYLE)


def header_block(latest_month_id: str, last_refresh_id: str) -> html.Div:
    return html.Div([
        html.Div([
            html.H2("Rate Response Dashboard",
                    className="mb-0",
                    style={"fontWeight": "600", "letterSpacing": "0.3px"}),
            html.Div("Monthly campaign volume, response and board performance",
                     style={"fontSize": "13px", "opacity": 0.85, "marginTop": "2px"}),
        ], style=HEADER_STYLE),
        html.Div([
            html.Span("● ", style={"color": "#27ae60"}),
            html.Span("Latest campaign month: "),
            html.Span(id=latest_month_id, style={"fontWeight": "600", "color": PRIMARY}),
            html.Span("    |    Last refresh: "),
            html.Span(id=last_refresh_id, style={"fontWeight": "600", "color": PRIMARY}),
        ], style=SUBTITLE_STYLE),
    ])


def section_header(text: str, sub: str | None = None) -> html.Div:
    children = [html.H5(text, style=SECTION_HEADER_STYLE)]
    if sub:
        children.append(html.Div(sub, className="text-muted small",
                                 style={"marginLeft": "14px", "marginBottom": "10px"}))
    return html.Div(children)


# ---------------------------------------------------------------- tab builders
def tab_executive() -> dbc.Tab:
    # Row 1 (headline): counts + the headline rate (actual_board_rate)
    kpis_row1 = dbc.Row([
        dbc.Col(kpi_card_dual("Total volume", "kpi-vol-latest", "kpi-vol-overall"), md=3),
        dbc.Col(kpi_card_dual("Responders", "kpi-resp-latest", "kpi-resp-overall"), md=3),
        dbc.Col(kpi_card_dual("Boards", "kpi-boards-latest", "kpi-boards-overall"), md=3),
        dbc.Col(kpi_card_dual("Actual board rate", "kpi-abr-latest", "kpi-abr-overall"), md=3),
    ], className="g-3")
    # Row 2 (calibration): response-rate family
    kpis_row2 = dbc.Row([
        dbc.Col(kpi_card_dual("Actual response rate", "kpi-arr-latest", "kpi-arr-overall"), md=3),
        dbc.Col(kpi_card_dual("Expected RR (TRM)", "kpi-trm-latest", "kpi-trm-overall"), md=3),
        dbc.Col(kpi_card_dual("Expected RR (XPM)", "kpi-xpm-latest", "kpi-xpm-overall"), md=3),
        dbc.Col(kpi_card_dual("Actual / Expected (TRM)", "kpi-aoe-latest", "kpi-aoe-overall"), md=3),
    ], className="g-3 mt-3")

    trend = dbc.Card(dbc.CardBody([
        html.H6("Monthly trend", className="mb-3", style={"color": PRIMARY}),
        dcc.Graph(id="exec-trend-chart", config={"displaylogo": False}),
    ]), className="mt-4", style={"borderRadius": "10px", "border": "none",
                                  "boxShadow": CARD_SHADOW})

    # Dynamic section header: shows the actual latest month (populated by callback)
    dynamic_header = html.Div([
        html.H5([
            "Latest month ",
            html.Span(id="exec-latest-month-badge",
                      className="badge",
                      style={"backgroundColor": ACCENT, "color": "#ffffff",
                             "fontSize": "14px", "padding": "4px 10px",
                             "borderRadius": "6px", "marginLeft": "4px",
                             "marginRight": "4px"}),
            " at a glance",
        ], style=SECTION_HEADER_STYLE),
        html.Div(
            "Big number is the latest month; smaller line is the all-month total.",
            className="text-muted small",
            style={"marginLeft": "14px", "marginBottom": "10px"},
        ),
    ])

    return dbc.Tab(
        label="Executive Summary",
        tab_id="tab-exec",
        label_style=TAB_LABEL_STYLE,
        children=html.Div([
            dynamic_header,
            kpis_row1,
            kpis_row2,
            section_header("Monthly trend"),
            trend,
        ], style=CONTENT_STYLE),
    )


def tab_pivot(cfg: dict) -> dbc.Tab:
    dims = cfg["catalog"]["dimensions"]
    metrics = cfg["catalog"]["metrics"]

    controls = dbc.Row([
        dbc.Col([
            html.Label("Row dimension", className="small fw-bold"),
            dcc.Dropdown(
                id="pivot-row-dim",
                options=[{"label": d, "value": d} for d in dims],
                value=cfg["dashboard"]["default_row_dim"],
                clearable=False,
            ),
        ], md=3),
        dbc.Col([
            html.Label("Metric (cells + line overlay)", className="small fw-bold"),
            dcc.Dropdown(
                id="pivot-metric",
                options=[{"label": m, "value": m} for m in metrics],
                value=cfg["dashboard"]["default_metric"],
                clearable=False,
            ),
        ], md=3),
        dbc.Col([
            html.Label("Suppress cells with volume <", className="small fw-bold"),
            dcc.Input(
                id="pivot-suppress",
                type="number",
                value=cfg["dashboard"]["small_cell_threshold"],
                min=0, step=10, style={"width": "100%"},
            ),
        ], md=2),
        dbc.Col([
            html.Label("Cell format", className="small fw-bold"),
            dcc.RadioItems(
                id="pivot-format",
                options=[{"label": " Number ", "value": "num"},
                         {"label": " Percent ", "value": "pct"}],
                value="pct",
                inline=True,
            ),
        ], md=4),
    ], className="g-3")

    filters = filter_block(cfg, prefix="pivot")

    table = dash_table.DataTable(
        id="pivot-table",
        page_size=50,
        style_table={"overflowX": "auto"},
        style_cell={"padding": "8px", "fontFamily": "Segoe UI, sans-serif",
                    "fontSize": "13px"},
        style_header={"backgroundColor": PRIMARY, "color": "#ffffff",
                      "fontWeight": "600"},
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#f8fafc"},
            {"if": {"filter_query": "{Row} = 'Overall'"},
             "backgroundColor": "#eaf3fb", "fontWeight": "700"},
        ],
    )

    combo = dbc.Card(dbc.CardBody([
        html.H6("Volume mix (stacked bars) + metric trend (line)",
                className="mb-3", style={"color": PRIMARY}),
        dcc.Graph(id="pivot-combo-chart", config={"displaylogo": False}),
    ]), className="mt-3", style={"borderRadius": "10px", "border": "none",
                                  "boxShadow": CARD_SHADOW})

    return dbc.Tab(
        label="Pivot View",
        tab_id="tab-pivot",
        label_style=TAB_LABEL_STYLE,
        children=html.Div([
            section_header("Controls"),
            controls,
            section_header("Filters"),
            filters,
            section_header("Pivot",
                           "Last row 'Overall' aggregates across the selected "
                           "row-dim values; last column 'Overall' aggregates across months."),
            html.Div(table, className="mt-2"),
            combo,
        ], style=CONTENT_STYLE),
    )


def tab_model(cfg: dict) -> dbc.Tab:
    filters = filter_block(cfg, prefix="model")

    by_vsband = dbc.Card(dbc.CardBody([
        html.H6("Actual vs Expected response rate by vs_band",
                className="mb-3", style={"color": PRIMARY}),
        dcc.Graph(id="model-by-vsband", config={"displaylogo": False}),
    ]), className="shadow-sm mb-3",
       style={"borderRadius": "10px", "border": "none"})

    monthly = dbc.Card(dbc.CardBody([
        html.H6("Actual RR vs Expected RR by month",
                className="mb-3", style={"color": PRIMARY}),
        dcc.Graph(id="model-arr-vs-exp", config={"displaylogo": False}),
    ]), className="shadow-sm mb-3",
       style={"borderRadius": "10px", "border": "none"})

    by_trm = dbc.Card(dbc.CardBody([
        html.H6("Actual RR by TRM10 tier",
                className="mb-3", style={"color": PRIMARY}),
        dcc.Graph(id="model-by-trm", config={"displaylogo": False}),
    ]), style={"borderRadius": "10px", "border": "none", "boxShadow": CARD_SHADOW})

    by_sc = dbc.Card(dbc.CardBody([
        html.H6("Actual / Expected by scorecard",
                className="mb-3", style={"color": PRIMARY}),
        dcc.Graph(id="model-by-scorecard", config={"displaylogo": False}),
    ]), style={"borderRadius": "10px", "border": "none", "boxShadow": CARD_SHADOW})

    return dbc.Tab(
        label="Model Performance",
        tab_id="tab-model",
        label_style=TAB_LABEL_STYLE,
        children=html.Div([
            section_header("Filters"),
            filters,
            section_header("Calibration"),
            by_vsband,
            monthly,
            dbc.Row([
                dbc.Col(by_trm, md=6),
                dbc.Col(by_sc, md=6),
            ], className="g-3"),
        ], style=CONTENT_STYLE),
    )


def tab_dq() -> dbc.Tab:
    table = dash_table.DataTable(
        id="dq-table",
        page_size=24,
        style_table={"overflowX": "auto"},
        style_cell={"padding": "8px", "fontFamily": "Segoe UI, sans-serif",
                    "fontSize": "13px"},
        style_header={"backgroundColor": PRIMARY, "color": "#ffffff",
                      "fontWeight": "600"},
        style_data_conditional=[
            {"if": {"row_index": "odd"}, "backgroundColor": "#f8fafc"},
            {"if": {"filter_query": "{has_xpm} = false",
                    "column_id": "has_xpm"},
             "backgroundColor": "#fdecea", "color": "#922b21"},
        ],
    )
    return dbc.Tab(
        label="Data Quality",
        tab_id="tab-dq",
        label_style=TAB_LABEL_STYLE,
        children=html.Div([
            section_header("Snapshot"),
            dbc.Row([
                dbc.Col(kpi_card("Latest month in mart", "dq-latest"), md=4),
                dbc.Col(kpi_card("Last refresh timestamp", "dq-last-refresh"), md=4),
                dbc.Col(kpi_card("Partitions present", "dq-partition-count"), md=4),
            ], className="g-3"),
            section_header("Per-month summary",
                           "has_xpm=false means expected_responses_xpm is null "
                           "for that month (SAS pipeline did not source EXP_RESPONSE_SCORE)."),
            table,
        ], style=CONTENT_STYLE),
    )


def tab_export(cfg: dict) -> dbc.Tab:
    return dbc.Tab(
        label="Export",
        tab_id="tab-export",
        label_style=TAB_LABEL_STYLE,
        children=html.Div([
            section_header("Download filtered view"),
            html.P("Choose filters, then download the aggregated rollup as CSV or Excel.",
                   className="text-muted small"),
            filter_block(cfg, prefix="export"),
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
                html.Li("expected_rr_trm = sum(expected_responses) / sum(volume)"),
                html.Li("expected_rr_xpm = sum(expected_responses_xpm) / sum(volume)"),
                html.Li("actual_board_rate = sum(Boards) / sum(volume)"),
                html.Li("actual_vs_expected_* = actual_response_rate / expected_rr_*"),
                html.Li("All rates are recomputed from sums. Rates are never averaged."),
            ], className="small"),
        ], style=CONTENT_STYLE),
    )


def filter_block(cfg: dict, prefix: str) -> html.Div:
    """Filters used by Pivot, Model Performance and Export tabs."""
    return html.Div([
        dbc.Row([
            dbc.Col([
                html.Label("Campaign months", className="small fw-bold"),
                dcc.Dropdown(id=f"{prefix}-f-months", multi=True, placeholder="all"),
            ], md=3),
            dbc.Col([
                html.Label("vs_band", className="small fw-bold"),
                dcc.Dropdown(id=f"{prefix}-f-vs", multi=True, placeholder="all"),
            ], md=2),
            dbc.Col([
                html.Label("scorecard", className="small fw-bold"),
                dcc.Dropdown(id=f"{prefix}-f-scorecard", multi=True, placeholder="all"),
            ], md=2),
            dbc.Col([
                html.Label("Prospect_type", className="small fw-bold"),
                dcc.Dropdown(id=f"{prefix}-f-prospect", multi=True, placeholder="all"),
            ], md=2),
            dbc.Col([
                html.Label("rm_flag", className="small fw-bold"),
                dcc.Dropdown(id=f"{prefix}-f-rm", multi=True, placeholder="all"),
            ], md=1),
            dbc.Col([
                html.Label("trm10_tier", className="small fw-bold"),
                dcc.Dropdown(id=f"{prefix}-f-trm", multi=True, placeholder="all"),
            ], md=2),
        ], className="g-2"),
        dbc.Row([
            dbc.Col([
                html.Label("annual_fee", className="small fw-bold"),
                dcc.Dropdown(id=f"{prefix}-f-fee", multi=True, placeholder="all"),
            ], md=3),
        ], className="g-2 mt-1"),
    ])


# ---------------------------------------------------------------- root
def build_layout(cfg: dict) -> html.Div:
    return html.Div([
        dcc.Store(id="cfg-store", data=cfg),
        header_block("subtitle-latest-month", "subtitle-last-refresh"),
        dbc.Tabs(
            [
                tab_executive(),
                tab_pivot(cfg),
                tab_model(cfg),
                tab_dq(),
                tab_export(cfg),
            ],
            id="tabs",
            active_tab="tab-exec",
            className="px-3",
        ),
    ], style={"backgroundColor": SUBTLE_BG, "minHeight": "100vh"})
