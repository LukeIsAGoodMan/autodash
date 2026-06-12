"""Top combination movers — Section D.

Enumerates the configured 2-way dim pairs, computes MoM NRR for each
(dim_A, dim_B, campaign_month) cell, and surfaces the largest movers above
a volume floor. A combination like (annual_fee=$75/$99, vs_band=550-600)
is much more diagnostic than either single dim alone — analysts spend
hours building these slices by hand in Excel, so this is where the report
saves real time.

This module reads the raw rollup mart directly because it needs cross-dim
slices that snapshot_builder doesn't pre-aggregate. The orchestrator
already loaded the mart once; we share that frame instead of re-reading.
"""
from __future__ import annotations

import polars as pl

from src import metrics

from .facts import ComboMovement, Direction, TopCombinationAnalysis


_FLAT_BPS = 1.0


def compute(rollup_df: pl.DataFrame, cfg: dict) -> TopCombinationAnalysis:
    ai_cfg = cfg.get("ai_agent", {})
    combo_cfg = ai_cfg.get("combinations", {})
    pairs = combo_cfg.get("dim_pairs", [])
    min_vol = int(combo_cfg.get("min_combo_volume", 5_000))
    top_k = int(combo_cfg.get("top_k", 8))

    if rollup_df.is_empty() or not pairs:
        return TopCombinationAnalysis(
            latest_month="", prior_month=None, min_volume=min_vol,
            pairs_evaluated=[],
        )

    months = sorted(rollup_df["campaign_month"].unique().to_list())
    if len(months) < 2:
        return TopCombinationAnalysis(
            latest_month=months[-1] if months else "",
            prior_month=None, min_volume=min_vol,
            pairs_evaluated=[f"{a} × {b}" for a, b in pairs],
        )
    latest, prior = months[-1], months[-2]

    candidates: list[ComboMovement] = []
    pairs_evaluated: list[str] = []
    for pair in pairs:
        if len(pair) != 2:
            continue
        dim_a, dim_b = pair[0], pair[1]
        if dim_a not in rollup_df.columns or dim_b not in rollup_df.columns:
            continue
        pair_label = f"{dim_a} × {dim_b}"
        pairs_evaluated.append(pair_label)
        candidates.extend(_pair_movers(rollup_df, dim_a, dim_b, latest, prior,
                                       min_vol, pair_label))

    # Split into gainers / losers, sorted by signed delta_bps so the most
    # extreme moves bubble up first.
    gainers = sorted(
        [c for c in candidates if c.delta_bps > _FLAT_BPS],
        key=lambda c: c.delta_bps, reverse=True,
    )[:top_k]
    losers = sorted(
        [c for c in candidates if c.delta_bps < -_FLAT_BPS],
        key=lambda c: c.delta_bps,
    )[:top_k]

    return TopCombinationAnalysis(
        latest_month=latest,
        prior_month=prior,
        min_volume=min_vol,
        pairs_evaluated=pairs_evaluated,
        top_gainers=gainers,
        top_losers=losers,
    )


def _pair_movers(
    rollup_df: pl.DataFrame, dim_a: str, dim_b: str,
    latest: str, prior: str, min_vol: int, pair_label: str,
) -> list[ComboMovement]:
    """Compute MoM movers for all (dim_a, dim_b) cells present in both months."""
    subset = rollup_df.filter(pl.col("campaign_month").is_in([latest, prior]))
    agg = metrics.aggregate_by(subset, group_dims=[dim_a, dim_b, "campaign_month"])

    by_cell: dict[tuple, dict[str, dict]] = {}
    for r in agg.iter_rows(named=True):
        key = (r[dim_a], r[dim_b])
        by_cell.setdefault(key, {})[r["campaign_month"]] = r

    movers: list[ComboMovement] = []
    for (va, vb), cells in by_cell.items():
        cur = cells.get(latest)
        prv = cells.get(prior)
        if cur is None or prv is None:
            continue
        if (cur["volume"] or 0) < min_vol or (prv["volume"] or 0) < min_vol:
            continue
        if cur["actual_response_rate"] is None or prv["actual_response_rate"] is None:
            continue
        delta = (cur["actual_response_rate"] - prv["actual_response_rate"]) * 10_000
        direction: Direction = ("up" if delta > _FLAT_BPS
                                else "down" if delta < -_FLAT_BPS
                                else "flat")
        movers.append(ComboMovement(
            dim_pair=pair_label,
            dim_values={dim_a: va, dim_b: vb},
            current_month=latest,
            prior_month=prior,
            current_nrr=float(cur["actual_response_rate"]),
            prior_nrr=float(prv["actual_response_rate"]),
            delta_bps=round(delta, 2),
            current_volume=float(cur["volume"] or 0),
            direction=direction,
        ))
    return movers
