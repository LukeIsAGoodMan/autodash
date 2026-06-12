"""MoM / YoY analysis layered on ReportFacts. Pure transformation — no I/O."""
from __future__ import annotations

from typing import Iterable

from .facts import Direction, KPIRow, Movement, MoMYoYAnalysis, ReportFacts


# Metrics the report tracks for MoM/YoY. Rate metrics report delta_bps;
# count metrics report delta_pct (relative change vs prior).
RATE_METRICS: tuple[str, ...] = ("nrr", "board_rate")
COUNT_METRICS: tuple[str, ...] = ("volume", "boards")
ALL_METRICS = RATE_METRICS + COUNT_METRICS

# Direction thresholds — anything under these is reported as 'flat' to avoid
# spurious "movements". 1bp is below noise floor for typical campaign sizes.
_FLAT_BPS = 1.0
_FLAT_PCT = 0.005  # 0.5%

# Default number of segment movers to surface (per dim).
BIGGEST_MOVERS_TOP_K = 5


def compute(facts: ReportFacts, top_k: int = BIGGEST_MOVERS_TOP_K) -> MoMYoYAnalysis:
    """Compute MoM, YoY, and biggest segment movers from ReportFacts."""
    if not facts.months_in_scope:
        return MoMYoYAnalysis(
            latest_month="",
            overall_mom=[],
            overall_yoy=[],
            product_mom=[],
            product_yoy=[],
            biggest_movers=[],
        )

    latest = facts.latest_month
    prior = _prior_month(facts.months_in_scope, latest)
    yoy_anchor = _yoy_anchor(facts.months_in_scope, latest)

    overall_mom = (
        _row_diffs(facts.overall_trend, latest, prior, period="MoM")
        if prior else []
    )
    overall_yoy = (
        _row_diffs(facts.overall_trend, latest, yoy_anchor, period="YoY")
        if yoy_anchor else []
    )

    product_mom = (
        _grouped_diffs(facts.product_trend, latest, prior, period="MoM")
        if prior else []
    )
    product_yoy = (
        _grouped_diffs(facts.product_trend, latest, yoy_anchor, period="YoY")
        if yoy_anchor else []
    )

    biggest = _biggest_movers(facts, top_k=top_k) if prior else []

    return MoMYoYAnalysis(
        latest_month=latest,
        overall_mom=overall_mom,
        overall_yoy=overall_yoy,
        product_mom=product_mom,
        product_yoy=product_yoy,
        biggest_movers=biggest,
    )


# ---------------------------------------------------------------- internals


def _prior_month(months: list[str], latest: str) -> str | None:
    """Return the month immediately before `latest` in `months`, or None."""
    if latest not in months:
        return None
    i = months.index(latest)
    return months[i - 1] if i > 0 else None


def _yoy_anchor(months: list[str], latest: str) -> str | None:
    """Return the YYYY-(MM-12) ISO month if present in `months`."""
    try:
        y, m = (int(x) for x in latest.split("-"))
    except ValueError:
        return None
    target = f"{y - 1:04d}-{m:02d}"
    return target if target in months else None


def _row_diffs(
    rows: Iterable[KPIRow],
    current_month: str,
    prior_month: str,
    period: str,
) -> list[Movement]:
    """Diff a single-keyed series (one row per month, dim='overall')."""
    cur = next((r for r in rows if r.campaign_month == current_month), None)
    prv = next((r for r in rows if r.campaign_month == prior_month), None)
    if cur is None or prv is None:
        return []
    return [
        _make_movement(cur.dim, cur.dim_value, m, period, current_month, prior_month,
                       _get(cur, m), _get(prv, m))
        for m in ALL_METRICS
    ]


def _grouped_diffs(
    rows: Iterable[KPIRow],
    current_month: str,
    prior_month: str,
    period: str,
) -> list[Movement]:
    """Diff per-dim_value series (e.g. one row per (product, month))."""
    by_value_cur: dict[str | None, KPIRow] = {}
    by_value_prv: dict[str | None, KPIRow] = {}
    dim_name: str | None = None
    for r in rows:
        dim_name = dim_name or r.dim
        if r.campaign_month == current_month:
            by_value_cur[r.dim_value] = r
        elif r.campaign_month == prior_month:
            by_value_prv[r.dim_value] = r
    out: list[Movement] = []
    # Union of values so a value present in only one month still appears
    # (with a None side that gets surfaced as direction='flat').
    for v in sorted(set(by_value_cur) | set(by_value_prv), key=lambda x: (x is None, x)):
        cur, prv = by_value_cur.get(v), by_value_prv.get(v)
        for m in ALL_METRICS:
            out.append(_make_movement(
                dim_name or "", v, m, period, current_month, prior_month,
                _get(cur, m) if cur else None,
                _get(prv, m) if prv else None,
            ))
    return out


def _biggest_movers(facts: ReportFacts, top_k: int) -> list[Movement]:
    """Rank segment-level NRR MoM movements by absolute bps change.

    Walks `segment_trend` (which carries all months) and isolates the two
    most-recent months for each dim. Only includes segments where both
    months have nrr defined (filters out small-cell-suppressed cells).
    """
    movers: list[Movement] = []
    for dim, rows in facts.segment_trend.items():
        months = sorted({r.campaign_month for r in rows})
        if len(months) < 2:
            continue
        latest_m, prior_m = months[-1], months[-2]
        latest = {r.dim_value: r for r in rows if r.campaign_month == latest_m}
        prior = {r.dim_value: r for r in rows if r.campaign_month == prior_m}
        for v, cur in latest.items():
            prv = prior.get(v)
            if prv is None or cur.nrr is None or prv.nrr is None:
                continue
            movers.append(_make_movement(
                dim, v, "nrr", "MoM", cur.campaign_month, prv.campaign_month,
                cur.nrr, prv.nrr,
            ))
    movers.sort(key=lambda m: abs(m.delta_bps or 0.0), reverse=True)
    return movers[:top_k]


def _make_movement(
    dim: str,
    dim_value: str | None,
    metric: str,
    period: str,
    current_month: str,
    prior_month: str,
    cur: float | None,
    prv: float | None,
) -> Movement:
    delta_abs: float | None = None
    delta_bps: float | None = None
    delta_pct: float | None = None
    direction: Direction = "flat"

    if cur is not None and prv is not None:
        delta_abs = cur - prv
        if metric in RATE_METRICS:
            delta_bps = round(delta_abs * 10_000, 2)
            if delta_bps > _FLAT_BPS:
                direction = "up"
            elif delta_bps < -_FLAT_BPS:
                direction = "down"
        else:
            delta_pct = (delta_abs / prv) if prv not in (0, None) else None
            if delta_pct is not None:
                if delta_pct > _FLAT_PCT:
                    direction = "up"
                elif delta_pct < -_FLAT_PCT:
                    direction = "down"
    return Movement(
        dim=dim,
        dim_value=dim_value,
        metric=metric,
        period=period,  # type: ignore[arg-type]
        current_month=current_month,
        prior_month=prior_month,
        current_value=cur,
        prior_value=prv,
        delta_abs=delta_abs,
        delta_bps=delta_bps,
        delta_pct=delta_pct,
        direction=direction,
    )


def _get(row: KPIRow, metric: str) -> float | None:
    return getattr(row, metric, None)
