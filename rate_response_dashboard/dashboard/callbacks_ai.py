"""Callbacks for the AI Report tab. Kept separate from the main callbacks
module so existing tabs stay untouched and the AI surface can be removed
or feature-flagged without diff noise."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from dash import Input, Output, State, html, no_update

from src.ai_agent.orchestrator import build_report_package
from src.ai_agent.report.renderer import render_html
from src.ai_agent.snapshot_builder import load_maturity

log = logging.getLogger(__name__)


REPORTS_DIR = Path("./reports")


def _maturity_chip(status: str | None, has_xpm: bool | None) -> html.Span:
    """Compact maturity indicator next to the month dropdown.

    Mirrors the chip styling used on the Data Quality tab so the analyst
    sees the same vocabulary in both places.
    """
    if status is None:
        return html.Span(
            "[maturity unknown — no validation_summary entry]",
            style={"fontSize": "11.5px", "color": "#8a6d3b",
                   "background": "#fff8e1", "padding": "2px 8px",
                   "borderRadius": "10px",
                   "border": "1px solid #d4a13a"},
        )
    s = status.lower()
    if s == "full":
        return html.Span(
            f"[full · XPM {'on' if has_xpm else 'off'}]",
            style={"fontSize": "11.5px", "color": "#137333",
                   "background": "#e6f4ea", "padding": "2px 8px",
                   "borderRadius": "10px",
                   "border": "1px solid #b7dfc4"},
        )
    # 'partial' or anything else: warn explicitly so the user sees BEFORE
    # they spend the LLM call that this month's NRR is still maturing.
    return html.Span(
        f"[{s} — NRR still maturing, treat report as preliminary]",
        style={"fontSize": "11.5px", "color": "#9e2a2a",
               "background": "#fef1f1", "padding": "2px 8px",
               "borderRadius": "10px",
               "border": "1px solid #b3434a"},
    )


def register_ai_callbacks(app, cfg: dict) -> None:
    # Populate the month dropdown when the AI Report tab becomes active.
    # Reading the mart on tab activation (rather than on app startup) means
    # newly-ingested months show up without a dashboard restart, matching
    # the existing tabs' cache-refresh behavior.
    @app.callback(
        Output("ai-month-select", "options"),
        Output("ai-month-select", "value"),
        Input("tabs", "active_tab"),
        State("ai-month-select", "value"),
        prevent_initial_call=False,
    )
    def _populate_months(active_tab, current_value):
        if active_tab != "tab-ai-report":
            return no_update, no_update
        try:
            maturity = load_maturity(cfg)
        except Exception as e:
            log.warning("month picker: failed to load maturity: %s", e)
            maturity = {}
        months = sorted(maturity.keys(), reverse=True)         # newest first
        if not months:
            return [], None
        opts = []
        for m in months:
            info = maturity.get(m)
            tag = ""
            if info and getattr(info, "status", None) == "partial":
                tag = "  (partial)"
            elif info and getattr(info, "status", None) == "unknown":
                tag = "  (unknown)"
            opts.append({"label": f"{m}{tag}", "value": m})
        # Default to latest month unless the user already picked something
        # that's still in the list (avoids overwriting an in-progress choice).
        value = current_value if current_value in {m for m in months} else months[0]
        return opts, value

    # Maturity chip — updates instantly when the user picks a different
    # month so they see the warning BEFORE clicking Generate.
    @app.callback(
        Output("ai-month-maturity", "children"),
        Input("ai-month-select", "value"),
        prevent_initial_call=False,
    )
    def _update_maturity_chip(month):
        if not month:
            return ""
        try:
            maturity = load_maturity(cfg)
        except Exception:
            return _maturity_chip(None, None)
        info = maturity.get(month)
        if info is None:
            return _maturity_chip(None, None)
        return _maturity_chip(getattr(info, "status", None),
                              getattr(info, "has_xpm", None))

    # Clientside callback: fires the instant the button is clicked, in the
    # browser, so the user sees "Generating…" + the button disable BEFORE
    # the long serverside work begins. Without this the UI looks frozen
    # because Python is busy computing and Dash can't push intermediate
    # updates. dcc.Loading provides the spinner overlay on the iframe;
    # this gives textual feedback alongside it.
    app.clientside_callback(
        """
        function(n_clicks) {
            if (!n_clicks) {
                return [window.dash_clientside.no_update,
                        window.dash_clientside.no_update];
            }
            return ['Generating report — this may take 20-40s with LLM enabled…',
                    true];
        }
        """,
        Output("ai-status", "children", allow_duplicate=True),
        Output("ai-generate-btn", "disabled", allow_duplicate=True),
        Input("ai-generate-btn", "n_clicks"),
        prevent_initial_call=True,
    )

    @app.callback(
        Output("ai-report-iframe", "srcDoc"),
        Output("ai-status", "children"),
        Output("ai-last-report-path", "data"),
        Output("ai-open-btn", "disabled"),
        Output("ai-generate-btn", "disabled"),
        Input("ai-generate-btn", "n_clicks"),
        State("ai-last-report-path", "data"),
        State("ai-month-select", "value"),
        prevent_initial_call=True,
    )
    def _generate(n_clicks, _prev_path, target_month):
        if not n_clicks:
            return no_update, no_update, no_update, no_update, no_update
        try:
            t0 = datetime.now()
            pkg = build_report_package(cfg, target_month=target_month)
            rendered = render_html(pkg)
            out_dir = REPORTS_DIR / t0.strftime("%Y%m%dT%H%M%S")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "report.html"
            out_path.write_text(rendered, encoding="utf-8")
            dt = (datetime.now() - t0).total_seconds()
            latest = pkg.facts.latest_month or "n/a"
            status = (f"Generated for {latest} in {dt:.1f}s. "
                      f"Saved to {out_path}.")
            # Re-enable Generate (last position); enable Open in new tab.
            return rendered, status, str(out_path), False, False
        except Exception as e:  # surface, don't swallow
            log.exception("AI report generation failed")
            err_html = (
                "<div style='font-family:Segoe UI;padding:24px;color:#b3434a'>"
                "<h3>Report generation failed</h3>"
                f"<pre style='background:#fbeceb;padding:12px;border-radius:4px'>{e}</pre>"
                "</div>"
            )
            return err_html, f"Failed: {e}", None, True, False
