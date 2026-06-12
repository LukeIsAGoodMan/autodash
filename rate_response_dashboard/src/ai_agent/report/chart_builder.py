"""Plotly figures → base64-encoded PNGs for HTML inlining.

Visual contract: matches the existing dashboard pivot view's combo pattern
exactly — stacked bars of volume by a dimension + overlay line of the
rate metric on a secondary y-axis. Analyst muscle memory carries straight
from the dashboard to the report.

Every chart function returns a base64 string (no `data:image/png;base64,`
prefix — the template adds it). On kaleido failure the function returns
None; the template renders a "chart unavailable" placeholder so a single
broken chart never tanks the whole report.

Import order intentionally pulls dashboard.plotly_template so the
omni_light template is registered as the Plotly default even when the
report is generated outside the Dash process (e.g. CLI smoke tests).
"""
from __future__ import annotations

import base64
import logging
from typing import Iterable, Sequence

import plotly.graph_objects as go

# Register omni_light as the Plotly default. Side-effect import.
from dashboard import plotly_template  # noqa: F401

from ..facts import (
    BigMacAnalysis,
    CalibrationPoint,
    KPIRow,
    ReportFacts,
    SliceTrendBundle,
    TopCombinationAnalysis,
)

log = logging.getLogger(__name__)


# Mirrors dashboard.plotly_template.OMNI_PALETTE for visual continuity.
PALETTE = [
    "#1a4d8c", "#4a7bb7", "#2e7a52", "#c98a25", "#b3434a",
    "#6b7d92", "#7e9bc0", "#4f6378", "#a9b8c8", "#384a5e",
]
LINE_COLOR = "#b3434a"   # rate-overlay color, matches pivot view "BAD" tone
MUTED = "#6b7d92"


def fig_to_base64(fig: go.Figure, width: int = 960, height: int = 380) -> str | None:
    """Render to PNG. Never raises — a chart failure must not break the report."""
    try:
        png_bytes = fig.to_image(format="png", width=width, height=height, scale=2)
        return base64.b64encode(png_bytes).decode("ascii")
    except Exception as e:
        log.warning("chart render failed: %s", e)
        return None


# ============================================================================
# Combo chart — stacked bars of volume by `dim` + overlay line of overall rate
# ============================================================================


def combo_volume_rate(
    rows: Iterable[KPIRow],
    dim_label: str,
    overall_trend: Sequence[KPIRow],
    rate_metric: str = "nrr",
    rate_label: str = "Overall NRR",
    title: str | None = None,
    top_n_values: int = 10,
) -> str | None:
    """Stacked bars (volume by dim_value, ordered by latest-month volume)
    + overlay line of overall NRR on the secondary y-axis.

    Long-tail dim_values are bucketed into "Other" so the legend stays
    readable — same trick the pivot view uses for high-cardinality dims.
    """
    rows = list(rows)
    if not rows:
        return None
    months = sorted({r.campaign_month for r in rows})
    if not months:
        return None
    latest = months[-1]

    # Rank dim_values by latest-month volume, take top_n, bucket rest as "Other".
    latest_by_v: dict = {}
    for r in rows:
        if r.campaign_month != latest:
            continue
        latest_by_v[r.dim_value] = (r.volume or 0) + latest_by_v.get(r.dim_value, 0)
    sorted_vs = sorted(latest_by_v.items(), key=lambda kv: kv[1], reverse=True)
    keep_set = {v for v, _ in sorted_vs[:top_n_values]}
    has_other = len(sorted_vs) > top_n_values

    # Aggregate volume by (canonical_value, month).
    series: dict = {}  # value → {month: volume}
    for r in rows:
        v = r.dim_value if r.dim_value in keep_set else ("Other" if r.dim_value is not None else "Other")
        series.setdefault(v, {})
        series[v][r.campaign_month] = (r.volume or 0) + series[v].get(r.campaign_month, 0)

    # Preserve legend order: top values by latest-month volume, then Other last.
    legend_order = [v for v, _ in sorted_vs[:top_n_values]]
    if has_other:
        legend_order.append("Other")

    fig = go.Figure()
    for i, v in enumerate(legend_order):
        ys = [series.get(v, {}).get(m, 0) for m in months]
        fig.add_bar(
            x=months, y=ys, name=str(v),
            marker_color=PALETTE[i % len(PALETTE)],
        )

    # Overlay line of overall rate on the secondary axis. Sourced from
    # overall_trend (the full mart aggregated over all dims), aligned to
    # the same `months` list so the x-axis lines up cleanly.
    overall_by_m = {r.campaign_month: r for r in overall_trend}
    line_y = [_get_metric(overall_by_m.get(m), rate_metric) for m in months]
    fig.add_scatter(
        x=months, y=line_y,
        mode="lines+markers", name=rate_label,
        yaxis="y2",
        line=dict(color=LINE_COLOR, width=2.5),
        marker=dict(size=7, line=dict(color="#ffffff", width=1.5)),
    )

    fig.update_layout(
        barmode="stack",
        title=dict(text=title, x=0.02, xanchor="left",
                   font=dict(size=14, color="#0e2238")) if title else None,
        xaxis=dict(title=None),
        yaxis=dict(title="Volume", tickformat=",.0f"),
        yaxis2=dict(
            title=rate_label, overlaying="y", side="right",
            tickformat=".2%", showgrid=False, rangemode="tozero",
        ),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, font=dict(size=11)),
        margin=dict(l=64, r=72, t=48, b=48),
    )
    return fig_to_base64(fig)


# ============================================================================
# Convenience wrappers — each section's main chart
# ============================================================================


def overall_combo(facts: ReportFacts) -> str | None:
    """Section A combo: stacked bars by annual_fee + overlay overall NRR."""
    rows = [r for r in facts.product_trend]
    return combo_volume_rate(
        rows=rows,
        dim_label="annual_fee",
        overall_trend=facts.overall_trend,
        title="Volume mix by product &amp; overall NRR",
    )


def slice_combo(rows: list[KPIRow], dim: str, facts: ReportFacts) -> str | None:
    """One combo per slice dim. Bars by dim_value, line of overall NRR."""
    return combo_volume_rate(
        rows=rows,
        dim_label=dim,
        overall_trend=facts.overall_trend,
        title=f"Volume mix by {dim} &amp; overall NRR",
    )


def big_mac_overall_combo(bm: BigMacAnalysis) -> str | None:
    """Big Mac overall: bars by drill_dim within the cohort + line of cohort NRR."""
    if bm.cohort_empty or not bm.by_drill_trend:
        return None
    return combo_volume_rate(
        rows=bm.by_drill_trend,
        dim_label=bm.drill_dim or "drill",
        overall_trend=bm.overall_trend,
        rate_label="Big Mac NRR",
        title=f"Big Mac — volume by {bm.drill_dim} &amp; cohort NRR",
    )


# ============================================================================
# Big Mac drill — multi-line of per-value NRR (no mix bars)
# ============================================================================


def big_mac_drill_trend(bm: BigMacAnalysis) -> str | None:
    """Per-vs_band NRR within Big Mac cohort. Multi-line — the point here
    is per-slice rate, not mix."""
    if bm.cohort_empty or not bm.by_drill_trend:
        return None
    rows = bm.by_drill_trend
    values = sorted({r.dim_value for r in rows if r.dim_value is not None})
    fig = go.Figure()
    for i, v in enumerate(values):
        ser = sorted([r for r in rows if r.dim_value == v], key=lambda r: r.campaign_month)
        fig.add_scatter(
            x=[r.campaign_month for r in ser],
            y=[r.nrr for r in ser],
            mode="lines+markers", name=str(v),
            line=dict(color=PALETTE[i % len(PALETTE)], width=2),
            marker=dict(size=6),
        )
    fig.update_layout(
        title=dict(text=f"Big Mac NRR by {bm.drill_dim} — per-slice rate",
                   x=0.02, xanchor="left", font=dict(size=14, color="#0e2238")),
        yaxis=dict(title="NRR", tickformat=".2%", rangemode="tozero"),
        xaxis=dict(title=None),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, font=dict(size=11)),
        margin=dict(l=64, r=24, t=48, b=48),
    )
    return fig_to_base64(fig)


# ============================================================================
# Top combination movers — diverging horizontal bar (gainers vs losers)
# ============================================================================


def top_combo_movers_chart(combos: TopCombinationAnalysis) -> str | None:
    """Horizontal bar: gainers (green, right) and losers (red, left), each
    bar labeled with the combination's dim_values. Most extreme movers at
    the top of each half."""
    if not combos.top_gainers and not combos.top_losers:
        return None

    # Stack losers on top (red), gainers below (green), so absolute biggest
    # appears furthest from the zero line — visually obvious.
    rows = []
    for m in combos.top_losers:
        rows.append((_combo_label(m), m.delta_bps, "loser"))
    for m in reversed(combos.top_gainers):
        rows.append((_combo_label(m), m.delta_bps, "gainer"))

    labels = [r[0] for r in rows]
    deltas = [r[1] for r in rows]
    colors = ["#b3434a" if r[2] == "loser" else "#2e7a52" for r in rows]

    fig = go.Figure()
    fig.add_bar(
        x=deltas, y=labels, orientation="h",
        marker_color=colors,
        text=[f"{d:+.0f} bps" for d in deltas],
        textposition="outside",
        textfont=dict(size=10),
    )
    fig.update_layout(
        title=dict(
            text=f"Top combination NRR movers ({combos.prior_month} → {combos.latest_month})",
            x=0.02, xanchor="left", font=dict(size=14, color="#0e2238"),
        ),
        xaxis=dict(title="Δ NRR (bps)", zeroline=True, zerolinecolor="#5a6573"),
        yaxis=dict(title=None, automargin=True),
        showlegend=False,
        margin=dict(l=120, r=64, t=48, b=48),
    )
    return fig_to_base64(fig, height=max(280, 28 * len(rows) + 80))


def _combo_label(m) -> str:
    """Render dim_values into a compact 'dim=val × dim=val' label."""
    parts = [f"{k}={v}" for k, v in m.dim_values.items()]
    return " × ".join(parts)


# ============================================================================
# Calibration trend — TRM vs XPM A/E over time
# ============================================================================


def calibration_trend(points: Sequence[CalibrationPoint]) -> str | None:
    if not points:
        return None
    months = sorted({p.campaign_month for p in points})
    fig = go.Figure()
    for model, color in (("TRM", "#1a4d8c"), ("XPM", "#b3434a")):
        ys = []
        for m in months:
            pt = next((p for p in points if p.campaign_month == m and p.model == model), None)
            ys.append(pt.ae_ratio if (pt and pt.available) else None)
        fig.add_scatter(
            x=months, y=ys,
            mode="lines+markers", name=f"{model} A/E",
            line=dict(color=color, width=2),
            marker=dict(size=6),
            connectgaps=False,
        )
    fig.add_hline(y=1.0, line=dict(color=MUTED, width=1, dash="dash"))
    fig.update_layout(
        title=dict(text="A/E calibration — TRM vs XPM",
                   x=0.02, xanchor="left", font=dict(size=14, color="#0e2238")),
        yaxis=dict(title="A/E ratio"),
        xaxis=dict(title=None),
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1, font=dict(size=11)),
        margin=dict(l=64, r=24, t=48, b=48),
    )
    return fig_to_base64(fig)


# ---------------------------------------------------------------- helpers


def _get_metric(row: KPIRow | None, metric: str) -> float | None:
    if row is None:
        return None
    return getattr(row, metric, None)
