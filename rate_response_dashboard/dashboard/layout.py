"""Static layout for the Dash app. All interactivity lives in callbacks.py.

The look-and-feel is deliberately calm and corporate: a dark slate header bar,
a thin subtitle with latest month + last refresh, then dbc.Tabs with five
panels. KPI cards use card-deck-style flex.
"""
from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import dash_table, dcc, html

# ---------------------------------------------------------------- style tokens
HEADER_STYLE = {
    "backgroundColor": "#1f2937",
    "color": "#f9fafb",
    "padding": "16px 24px",
    "marginBottom": "0",
}
SUBTITLE_STYLE = {
    "backgroundColor": "#374151",
    "color": "#e5e7eb",
    "padding": "8px 24px",
    "fontSize": "13px",
    "marginBottom": "16px",
}
KPI_CARD_STYLE = {"minHeight": "110px"}
CONTENT_STYLE = {"padding": "0 24px 24px 24px"}


# ---------------------------------------------------------------- helpers
def kpi_card(title: str, kpi_id: str, sub_id: str | None = None) -> dbc.Card:
    body = [
        html.Div(title, className="text-muted small text-uppercase"),
        html.Div(id=kpi_id, className="h3 mb-0"),
    ]
    if sub_id:
        body.append(html.Div(id=sub_id, className="text-muted small"))
    return dbc.Card(dbc.CardBody(body), style=KPI_CARD_STYLE, className="shadow-sm")


def header_block(latest_month_id: str, last_refresh_id: str) -> html.Div:
    return html.Div([
        html.Div([
            html.H3("Rate Response Dashboard", className="mb-0"),
        ], style=HEADER_STYLE),
        html.Div([
            html.Span("Latest campaign month: "),
            html.Span(id=latest_month_id, style={"fontWeight": "600"}),
            html.Span("   |   Last refresh: "),
            html.Span(id=last_refresh_id, style={"fontWeight": "600"}),
        ], style=SUBTITLE_STYLE),
    ])


# ---------------------------------------------------------------- tab builders
def tab_executive() -> dbc.Tab:
    kpis = dbc.Row([
        dbc.Col(kpi_card("Total volume", "kpi-volume"), md=3),
        dbc.Col(kpi_card("Responders", "kpi-responders"), md=3),
        dbc.Col(kpi_card("Boards", "kpi-boards"), md=3),
        dbc.Col(kpi_card("Actual response rate", "kpi-arr"), md=3),
    ], className="g-3")
    kpis2 = dbc.Row([
        dbc.Col(kpi_card("Expected RR (TRM)", "kpi-exp-trm"), md=3),
        dbc.Col(kpi_card("Expected RR (XPM)", "kpi-exp-xpm"), md=3),
        dbc.Col(kpi_card("Actual / Expected (TRM)", "kpi-aoe-trm"), md=3),
        dbc.Col(kpi_card("Board rate", "kpi-board-rate"), md=3),
    ], className="g-3 mt-2")

    trend = dbc.Card(dbc.CardBody([
        html.H6("Monthly trend"),
        dcc.Graph(id="exec-trend-chart", config={"displaylogo": False}),
    ]), className="mt-3 shadow-sm")

    return dbc.Tab(
        label="Executive Summary",
        tab_id="tab-exec",
        children=html.Div([kpis, kpis2, trend], style=CONTENT_STYLE),
    )


def tab_pivot(cfg: dict) -> dbc.Tab:
    dims = cfg["catalog"]["dimensions"]
    metrics = cfg["catalog"]["metrics"]

    controls = dbc.Row([
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
            html.Label("Metric"),
            dcc.Dropdown(
                id="pivot-metric",
                options=[{"label": m, "value": m} for m in metrics],
                value=cfg["dashboard"]["default_metric"],
                clearable=False,
            ),
        ], md=3),
        dbc.Col([
            html.Label("Suppress cells with volume <"),
            dcc.Input(
                id="pivot-suppress",
                type="number",
                value=cfg["dashboard"]["small_cell_threshold"],
                min=0, step=10, style={"width": "100%"},
            ),
        ], md=2),
        dbc.Col([
            html.Label("Format"),
            dcc.RadioItems(
                id="pivot-format",
                options=[{"label": "Number", "value": "num"},
                         {"label": "Percent", "value": "pct"}],
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
        style_cell={"padding": "6px", "fontFamily": "Segoe UI, sans-serif"},
        style_header={"backgroundColor": "#1f2937", "color": "#f9fafb", "fontWeight": "600"},
    )

    return dbc.Tab(
        label="Pivot View",
        tab_id="tab-pivot",
        children=html.Div([
            controls,
            html.Hr(),
            filters,
            html.Div(table, className="mt-3"),
        ], style=CONTENT_STYLE),
    )


def tab_model() -> dbc.Tab:
    g1 = dcc.Graph(id="model-arr-vs-exp", config={"displaylogo": False})
    g2 = dcc.Graph(id="model-by-trm", config={"displaylogo": False})
    g3 = dcc.Graph(id="model-by-scorecard", config={"displaylogo": False})

    return dbc.Tab(
        label="Model Performance",
        tab_id="tab-model",
        children=html.Div([
            dbc.Card(dbc.CardBody([html.H6("Actual RR vs Expected RR by month"), g1]),
                     className="shadow-sm mb-3"),
            dbc.Row([
                dbc.Col(dbc.Card(dbc.CardBody([html.H6("Actual RR by TRM10 tier"), g2]),
                                 className="shadow-sm"), md=6),
                dbc.Col(dbc.Card(dbc.CardBody([html.H6("Actual / Expected by scorecard"), g3]),
                                 className="shadow-sm"), md=6),
            ], className="g-3"),
        ], style=CONTENT_STYLE),
    )


def tab_dq() -> dbc.Tab:
    table = dash_table.DataTable(
        id="dq-table",
        page_size=24,
        style_table={"overflowX": "auto"},
        style_cell={"padding": "6px", "fontFamily": "Segoe UI, sans-serif"},
        style_header={"backgroundColor": "#1f2937", "color": "#f9fafb", "fontWeight": "600"},
    )
    return dbc.Tab(
        label="Data Quality",
        tab_id="tab-dq",
        children=html.Div([
            dbc.Row([
                dbc.Col(kpi_card("Latest month in mart", "dq-latest"), md=4),
                dbc.Col(kpi_card("Last refresh timestamp", "dq-last-refresh"), md=4),
                dbc.Col(kpi_card("Partitions present", "dq-partition-count"), md=4),
            ], className="g-3"),
            html.H6("Per-month summary", className="mt-4"),
            table,
        ], style=CONTENT_STYLE),
    )


def tab_export(cfg: dict) -> dbc.Tab:
    return dbc.Tab(
        label="Export",
        tab_id="tab-export",
        children=html.Div([
            html.P("Download the current filtered + aggregated view as CSV or Excel."),
            filter_block(cfg, prefix="export"),
            html.Div([
                dbc.Button("Download CSV", id="btn-export-csv", color="primary", className="me-2"),
                dbc.Button("Download Excel", id="btn-export-xlsx", color="secondary"),
            ], className="mt-3"),
            dcc.Download(id="export-download"),
            html.Hr(),
            html.Details([
                html.Summary("Metric definitions"),
                html.Ul([
                    html.Li("actual_response_rate = sum(responders) / sum(volume)"),
                    html.Li("expected_rr_trm = sum(expected_responses) / sum(volume)"),
                    html.Li("expected_rr_xpm = sum(expected_responses_xpm) / sum(volume)"),
                    html.Li("board_rate = sum(Boards) / sum(volume)"),
                    html.Li("actual_vs_expected_* = actual_response_rate / expected_rr_*"),
                    html.Li("All rates are recomputed from sums. Do NOT average rates."),
                ]),
            ]),
        ], style=CONTENT_STYLE),
    )


def filter_block(cfg: dict, prefix: str) -> html.Div:
    """Filters used by Pivot and Export tabs. Component ids are namespaced
    by `prefix` so the same set of filters can appear in multiple tabs."""
    return html.Div([
        dbc.Row([
            dbc.Col([
                html.Label("Campaign months"),
                dcc.Dropdown(id=f"{prefix}-f-months", multi=True, placeholder="all"),
            ], md=3),
            dbc.Col([
                html.Label("vs_band"),
                dcc.Dropdown(id=f"{prefix}-f-vs", multi=True, placeholder="all"),
            ], md=2),
            dbc.Col([
                html.Label("scorecard"),
                dcc.Dropdown(id=f"{prefix}-f-scorecard", multi=True, placeholder="all"),
            ], md=2),
            dbc.Col([
                html.Label("Prospect_type"),
                dcc.Dropdown(id=f"{prefix}-f-prospect", multi=True, placeholder="all"),
            ], md=2),
            dbc.Col([
                html.Label("rm_flag"),
                dcc.Dropdown(id=f"{prefix}-f-rm", multi=True, placeholder="all"),
            ], md=1),
            dbc.Col([
                html.Label("trm10_tier"),
                dcc.Dropdown(id=f"{prefix}-f-trm", multi=True, placeholder="all"),
            ], md=2),
        ], className="g-2"),
        dbc.Row([
            dbc.Col([
                html.Label("annual_fee"),
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
                tab_model(),
                tab_dq(),
                tab_export(cfg),
            ],
            id="tabs",
            active_tab="tab-exec",
        ),
    ])
