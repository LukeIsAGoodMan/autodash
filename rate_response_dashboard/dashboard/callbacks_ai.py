"""Callbacks for the AI Report tab. Kept separate from the main callbacks
module so existing tabs stay untouched and the AI surface can be removed
or feature-flagged without diff noise."""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from dash import Input, Output, State, no_update

from src.ai_agent.orchestrator import build_report_package
from src.ai_agent.report.renderer import render_html

log = logging.getLogger(__name__)


REPORTS_DIR = Path("./reports")


def register_ai_callbacks(app, cfg: dict) -> None:
    @app.callback(
        Output("ai-report-iframe", "srcDoc"),
        Output("ai-status", "children"),
        Output("ai-last-report-path", "data"),
        Output("ai-open-btn", "disabled"),
        Input("ai-generate-btn", "n_clicks"),
        State("ai-last-report-path", "data"),
        prevent_initial_call=True,
    )
    def _generate(n_clicks, _prev_path):
        if not n_clicks:
            return no_update, no_update, no_update, no_update
        try:
            t0 = datetime.now()
            pkg = build_report_package(cfg)
            html = render_html(pkg)
            out_dir = REPORTS_DIR / t0.strftime("%Y%m%dT%H%M%S")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / "report.html"
            out_path.write_text(html, encoding="utf-8")
            dt = (datetime.now() - t0).total_seconds()
            latest = pkg.facts.latest_month or "n/a"
            status = (f"Generated for {latest} in {dt:.1f}s. "
                      f"Saved to {out_path}.")
            return html, status, str(out_path), False
        except Exception as e:  # surface, don't swallow
            log.exception("AI report generation failed")
            err_html = (
                "<div style='font-family:Segoe UI;padding:24px;color:#b3434a'>"
                "<h3>Report generation failed</h3>"
                f"<pre style='background:#fbeceb;padding:12px;border-radius:4px'>{e}</pre>"
                "</div>"
            )
            return err_html, f"Failed: {e}", None, True
