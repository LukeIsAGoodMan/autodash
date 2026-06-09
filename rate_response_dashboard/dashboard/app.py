"""Dash app entrypoint. Run with:

    python -m dashboard.app
or
    python scripts/run_dashboard.py
"""
from __future__ import annotations

import os
import sys

import dash
import dash_bootstrap_components as dbc

# Allow running as `python dashboard/app.py` from project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard.callbacks import register_callbacks  # noqa: E402
from dashboard.layout import build_layout            # noqa: E402
from src.utils import load_config, setup_logging     # noqa: E402


def create_app(cfg: dict | None = None) -> dash.Dash:
    cfg = cfg or load_config()
    setup_logging(cfg)
    app = dash.Dash(
        __name__,
        title=cfg["dashboard"]["title"],
        # FLATLY: corporate-clean Bootswatch theme; same dbc components.
        external_stylesheets=[dbc.themes.FLATLY],
        suppress_callback_exceptions=True,
    )
    app.layout = build_layout(cfg)
    register_callbacks(app, cfg)
    return app


def main() -> None:
    app = create_app()
    # Default bind to 0.0.0.0:8050 so internal users on the network can hit it.
    # Override via env vars if your shop wants something tighter.
    host = os.environ.get("DASH_HOST", "0.0.0.0")
    port = int(os.environ.get("DASH_PORT", "8050"))
    debug = os.environ.get("DASH_DEBUG", "false").lower() == "true"
    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
