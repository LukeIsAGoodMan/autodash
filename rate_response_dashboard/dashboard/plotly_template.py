"""Plotly template for the dashboard.

Registers a template named 'omni_light' and sets it as the default so every
figure created without an explicit template argument picks it up. Matches the
blue/white brand palette in assets/custom.css.

Side effect: importing this module mutates plotly.io.templates. Import once
from dashboard/app.py.
"""
from __future__ import annotations

import plotly.graph_objects as go
import plotly.io as pio

# Palette used by every chart. First color is the headline brand blue.
OMNI_PALETTE = [
    "#1a4d8c",   # primary corporate blue (headline)
    "#4a7bb7",   # accent blue
    "#2e7a52",   # good (green)
    "#c98a25",   # warn (amber)
    "#b3434a",   # bad (red)
    "#6b7d92",   # muted slate
    "#7e9bc0",   # light accent
    "#4f6378",   # deep slate
    "#a9b8c8",   # pale slate
    "#384a5e",   # dark accent
]

# Token colors (kept in sync with custom.css :root variables)
INK = "#0e2238"
INK_SOFT = "#2c4663"
MUTED = "#6b7d92"
LINE = "#dde3ea"
LINE_SOFT = "#eef1f5"
INK_CHIP = "#1c2a3a"

omni_light = go.layout.Template(
    layout=dict(
        font=dict(
            family='"Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, '
                   '"Helvetica Neue", Arial, sans-serif',
            size=12,
            color=INK,
        ),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        colorway=OMNI_PALETTE,
        xaxis=dict(
            showgrid=False,
            zeroline=False,
            ticks="outside",
            ticklen=4,
            tickcolor=LINE,
            tickfont=dict(color=MUTED, size=11),
            linecolor=LINE,
            title=dict(font=dict(color=INK_SOFT, size=11)),
        ),
        yaxis=dict(
            showgrid=True,
            gridcolor=LINE_SOFT,
            gridwidth=1,
            zeroline=False,
            ticks="outside",
            ticklen=4,
            tickcolor=LINE,
            tickfont=dict(color=MUTED, size=11),
            linecolor=LINE,
            title=dict(font=dict(color=INK_SOFT, size=11)),
        ),
        yaxis2=dict(
            showgrid=False,
            zeroline=False,
            tickfont=dict(color=MUTED, size=11),
            title=dict(font=dict(color=INK_SOFT, size=11)),
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=11, color=INK_SOFT),
            bgcolor="rgba(0,0,0,0)",
            bordercolor="rgba(0,0,0,0)",
        ),
        margin=dict(l=12, r=12, t=24, b=12),
        hovermode="x unified",
        hoverlabel=dict(
            bgcolor=INK_CHIP,
            bordercolor=INK_CHIP,
            font=dict(
                color="#eef3ee",
                size=12,
                family='"Segoe UI", sans-serif',
            ),
            align="left",
        ),
    )
)


def register() -> None:
    """Install omni_light as the default plotly template."""
    pio.templates["omni_light"] = omni_light
    pio.templates.default = "omni_light"


# Register on import so app.py can simply `import dashboard.plotly_template`.
register()
