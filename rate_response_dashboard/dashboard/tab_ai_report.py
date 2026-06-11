"""AI Report Generator tab.

Stage 1: a single button triggers the deterministic pipeline
(snapshot_builder → mom_yoy → model_compare → HTML render). The HTML is
displayed inline via an Iframe whose srcDoc is set by the callback.
The generated artifact is also saved under ./reports/<timestamp>/ so
analysts can re-open or attach to email.

When Stage 2+ adds LLM stages, this tab grows a progress tracker; for now
it's just one button and one preview pane — keep the surface small until
the LLM dependency lands.
"""
from __future__ import annotations

import dash_bootstrap_components as dbc
from dash import dcc, html


def tab_ai_report() -> dbc.Tab:
    controls = html.Div([
        html.Div([
            dbc.Button("Generate report", id="ai-generate-btn",
                       color="primary", className="me-2"),
            dbc.Button("Open in new tab", id="ai-open-btn",
                       color="secondary", outline=True, disabled=True),
            html.Span(id="ai-status", className="chart-meta",
                      style={"marginLeft": "12px", "color": "#5a6573"}),
        ], style={"display": "flex", "alignItems": "center"}),
        html.Div(
            "Stage 1 — deterministic pipeline (no LLM). "
            "Reads the existing rollup + decile marts and emits an HTML report.",
            className="chart-meta",
            style={"marginTop": "6px", "color": "#5a6573"},
        ),
    ], className="controls-bar")

    preview = dbc.Card(
        dbc.CardBody([
            html.H6("Report preview"),
            html.Iframe(
                id="ai-report-iframe",
                srcDoc="<div style='font-family:Segoe UI;padding:24px;color:#5a6573'>"
                       "Click <strong>Generate report</strong> to build the latest report.</div>",
                style={"width": "100%", "height": "1200px", "border": "1px solid #e1e4e8"},
            ),
        ]),
        className="mb-3",
    )

    # Hidden store keeps the last generated HTML around so a second click on
    # "Open in new tab" can pop a window without regenerating.
    return dbc.Tab(
        label="AI Report",
        tab_id="tab-ai-report",
        children=html.Div([
            controls,
            preview,
            dcc.Store(id="ai-last-report-path"),
        ], style={"padding": "12px"}),
    )
