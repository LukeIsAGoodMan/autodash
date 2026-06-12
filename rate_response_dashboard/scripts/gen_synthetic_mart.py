"""Generate synthetic parquet marts so the AI report pipeline can be
smoke-tested locally without company data.

Distributions are tuned to match the real 2025-02 rollup fixture
(`tests/fixtures/rollup.parquet`) — column dtypes, dim cardinalities,
volume scale, and per-product NRR are calibrated against that snapshot.
Numbers are still random with deterministic seed; they are NOT
representative of real campaigns and must not be used for any business
decision.

The XPM data gap is modeled deliberately: months BEFORE 2026-03 emit
`expected_responses_xpm = None` (matching the SAS gap behavior we
observed in real 2025-02 data). Months from 2026-03 onward emit a
calibrated XPM value so `has_xpm` flips and the calibration trend chart
shows the gap → coverage transition.

Run from project root:

    python scripts/gen_synthetic_mart.py
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import load_config


# ============================================================================
# Real-data-derived constants. Each comment ties back to a fact in the
# 7019-row 2025-02 rollup fixture so future tweaks can be reasoned about.
# ============================================================================

# annual_fee strings include spaces in the real mart ("$75 / $99" not
# "$75/$99"). Anything that joins on this column must use the spaced form.
ANNUAL_FEES = ["$75 / $99", "$95 / $95", "$0 / $0", "$75 / $75"]
# Row-count weights from real fixture (4356 / 2003 / 315 / 345 out of 7019).
ANNUAL_FEE_WEIGHTS = [0.62, 0.285, 0.045, 0.05]

# Prospect_type has only TWO values in the real mart, not five.
PROSPECT_TYPES = ["Prospecting", "Retargeting"]
PROSPECT_TYPE_WEIGHTS = [0.19, 0.81]   # 1351 / 5668

VS_BANDS = ["530-549", "550-600", "601-639", "640-700", "701-730", "731-830"]
# Approx row weights from real: 824 / 2016 / 2688 / 1236 / 247 / 8.
VS_BAND_WEIGHTS = [0.12, 0.29, 0.38, 0.18, 0.03, 0.001]

# Real scorecard values are 1-4, not 11-15 as my earlier draft assumed.
SCORECARDS = [1, 2, 3, 4]
SCORECARD_WEIGHTS = [0.16, 0.03, 0.62, 0.18]   # 1144 / 207 / 4384 / 1284

TRM10_TIERS = list(range(1, 41))               # peaks around 4-8 in real
TIMES_MAILED = list(range(0, 15))              # 0-14, mostly 0-12
RM_FLAG = [0, 1]
RM_FLAG_WEIGHTS = [0.72, 0.28]                  # 5084 / 1935

# Each flag is independently 25-30% "1" in real.
FLAG_VALS = [0, 1]
FLAG_WEIGHTS = {
    "pqabandon_flag":   [0.91, 0.09],   # 6360 / 659
    "prchargeoff_flag": [0.73, 0.27],   # 5118 / 1901
    "prclosure_flag":   [0.82, 0.18],   # 5787 / 1232
    "prdecline_flag":   [0.73, 0.27],   # 5143 / 1876
}

# Per-row volume — lognormal calibrated so p50≈340, max≈150k, mean≈3k.
# Real numbers: min=1, p50=336, max=148352, mean=3224.
VOLUME_LN_MU = 5.82
VOLUME_LN_SIGMA = 2.1
VOLUME_MAX = 200_000

# NRR baseline per product, taken from real fixture (responders/volume by AF).
PRODUCT_BASE_NRR = {
    "$75 / $99":  0.0140,    # 194354 / 13.9M
    "$95 / $95":  0.0095,    # 75332 / 7.9M
    "$0 / $0":    0.0107,    # 7447 / 696k
    "$75 / $75":  0.0085,    # 876 / 102k
}

# vs_band has a real bias too — middle bands tend to respond best in
# untouched cohorts. Used as a small additive nudge to per-row NRR.
VS_BAND_NRR_BIAS = {
    "530-549": -0.001,
    "550-600":  0.001,
    "601-639":  0.002,
    "640-700":  0.000,
    "701-730": -0.001,
    "731-830": -0.002,
}

# In real 2025-02, ~24% of rollup rows had null Boards. We mirror that.
BOARDS_NULL_PROB = 0.24

# Real 2025-02 A/E TRM was 0.836 — model was over-predicting. Synthetic
# applies a similar bias by setting expected slightly higher than actual.
EXP_TRM_BIAS_MEAN = 1.18    # expected = actual × 1.18 → A/E ≈ 0.85
EXP_TRM_BIAS_JITTER = 0.08

# XPM cutover: months strictly before this ISO yield null xpm. From this
# month forward, xpm is populated and well-calibrated (A/E around 1.0).
XPM_AVAILABLE_FROM = "2026-03"
EXP_XPM_BIAS_MEAN = 1.02
EXP_XPM_BIAS_JITTER = 0.05


# ============================================================================
# Row generation
# ============================================================================

def _flag(rng: random.Random, name: str) -> int:
    return rng.choices(FLAG_VALS, weights=FLAG_WEIGHTS[name])[0]


def _gen_volume(rng: random.Random) -> int:
    v = int(rng.lognormvariate(VOLUME_LN_MU, VOLUME_LN_SIGMA))
    return max(1, min(v, VOLUME_MAX))


def _make_row(cm_iso: str, rng: random.Random, month_drift: float = 0.0,
              has_xpm: bool = False) -> dict:
    """Generate one rollup row. `month_drift` is a per-row NRR adjustment used
    to inject a deliberate trend across months. `has_xpm` controls whether
    expected_responses_xpm is populated for this row's month."""
    product = rng.choices(ANNUAL_FEES, weights=ANNUAL_FEE_WEIGHTS)[0]
    vb = rng.choices(VS_BANDS, weights=VS_BAND_WEIGHTS)[0]
    prospect = rng.choices(PROSPECT_TYPES, weights=PROSPECT_TYPE_WEIGHTS)[0]

    vol = _gen_volume(rng)
    base = PRODUCT_BASE_NRR[product] + VS_BAND_NRR_BIAS[vb] + month_drift
    base += rng.uniform(-0.0008, 0.0008)
    nrr = max(0.0005, base)
    responders = round(vol * nrr)
    boards = None if rng.random() < BOARDS_NULL_PROB else round(responders * rng.uniform(0.65, 0.85))

    exp_t = responders * rng.uniform(
        EXP_TRM_BIAS_MEAN - EXP_TRM_BIAS_JITTER,
        EXP_TRM_BIAS_MEAN + EXP_TRM_BIAS_JITTER,
    )
    exp_x: float | None
    if has_xpm:
        exp_x = responders * rng.uniform(
            EXP_XPM_BIAS_MEAN - EXP_XPM_BIAS_JITTER,
            EXP_XPM_BIAS_MEAN + EXP_XPM_BIAS_JITTER,
        )
    else:
        exp_x = None

    return {
        "campaign_month": cm_iso,
        "Prospect_type": prospect,
        "pqabandon_flag":   _flag(rng, "pqabandon_flag"),
        "prchargeoff_flag": _flag(rng, "prchargeoff_flag"),
        "prclosure_flag":   _flag(rng, "prclosure_flag"),
        "prdecline_flag":   _flag(rng, "prdecline_flag"),
        "vs_band": vb,
        "annual_fee": product,
        "times_mailed_12mo_cnt": rng.choice(TIMES_MAILED),
        "trm10_tier": rng.choice(TRM10_TIERS),
        "scorecard": rng.choices(SCORECARDS, weights=SCORECARD_WEIGHTS)[0],
        "rm_flag": rng.choices(RM_FLAG, weights=RM_FLAG_WEIGHTS)[0],
        "volume": float(vol),
        "responders": float(responders),
        "Boards": (float(boards) if boards is not None else None),
        "expected_responses": float(exp_t),
        "expected_responses_xpm": (float(exp_x) if exp_x is not None else None),
    }


def gen_rollup_partition(cm_iso: str, rng: random.Random, month_idx: int,
                         total_months: int) -> pl.DataFrame:
    """One partition. ~7000 rows matching real fixture row count.

    NRR drift across the lookback creates a visible trend in MoM/YoY charts.
    The shape is a mild U: declining from start through mid-window, then
    recovering — typical "model staleness then re-calibration" story.
    """
    has_xpm = cm_iso >= XPM_AVAILABLE_FROM
    # Mild U-shape so charts look interesting: -3bps to 0 then back to +2bps.
    mid = total_months / 2.0
    drift = (abs(month_idx - mid) / mid - 0.5) * 0.002   # in [-1, +1] * 0.002

    rows = [_make_row(cm_iso, rng, month_drift=drift, has_xpm=has_xpm)
            for _ in range(7_000)]

    # Real fixture had only 12 Big Mac rows (0.17%); we mirror by NOT injecting
    # extra Big Mac rows. The natural draw produces ~5-10 per month from the
    # joint probability of (Prospecting × rm_flag=0 × tm=0 × tier in {1,21}).
    return pl.DataFrame(rows)


# ============================================================================
# Decile marts (unchanged shape — KS/AUC/Gini need rank-ordered deciles)
# ============================================================================

def gen_decile_port_partition(cm_iso: str, rng: random.Random) -> pl.DataFrame:
    rows = []
    total_vol = 1_000_000
    per_dec = total_vol / 20
    for d in range(1, 21):
        target_rr = 0.025 * (1.0 - (d - 1) / 19) ** 1.3 + 0.003
        target_rr *= rng.uniform(0.9, 1.1)
        vol = per_dec * rng.uniform(0.95, 1.05)
        resp = int(vol * target_rr)
        boards = int(resp * rng.uniform(0.75, 0.85))
        rows.append({"campaign_month": cm_iso, "decile": d,
                     "volume": float(vol), "responders": float(resp),
                     "Boards": float(boards)})
    return pl.DataFrame(rows)


def gen_decile_sc_partition(cm_iso: str, rng: random.Random) -> pl.DataFrame:
    rows = []
    for sc in SCORECARDS:
        total = 200_000
        per_dec = total / 10
        for d in range(1, 11):
            target_rr = 0.022 * (1.0 - (d - 1) / 9) ** 1.3 + 0.004
            target_rr *= rng.uniform(0.85, 1.15)
            vol = per_dec * rng.uniform(0.9, 1.1)
            resp = int(vol * target_rr)
            boards = int(resp * rng.uniform(0.75, 0.85))
            rows.append({"campaign_month": cm_iso, "scorecard": sc, "decile": d,
                         "volume": float(vol), "responders": float(resp),
                         "Boards": float(boards)})
    return pl.DataFrame(rows)


# ============================================================================
# Driver
# ============================================================================

def _months(n: int) -> list[str]:
    """Most recent n months ISO, oldest first. Calendar-correct."""
    today = datetime.utcnow().replace(day=1)
    y, m = today.year, today.month
    out: list[str] = []
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return list(reversed(out))


def main() -> None:
    cfg = load_config()
    rng = random.Random(20260611)   # deterministic so re-runs are stable

    mart_dir = Path(cfg["paths"]["mart_dir"])
    dport_dir = Path(cfg["paths"]["decile_port_mart_dir"])
    dsc_dir = Path(cfg["paths"]["decile_sc_mart_dir"])
    logs_dir = Path(cfg["paths"]["logs_dir"])

    months = _months(15)
    print(f"Generating {len(months)} months: {months[0]} → {months[-1]}")
    print(f"XPM data available from {XPM_AVAILABLE_FROM} onward.")

    for idx, cm in enumerate(months):
        # Rollup mart with real-distribution-matching dims + XPM cutover.
        df = gen_rollup_partition(cm, rng, idx, len(months))
        part = mart_dir / f"campaign_month={cm}"
        part.mkdir(parents=True, exist_ok=True)
        df.write_parquet(part / "rollup.parquet")

        df = gen_decile_port_partition(cm, rng)
        part = dport_dir / f"campaign_month={cm}"
        part.mkdir(parents=True, exist_ok=True)
        df.write_parquet(part / "decile.parquet")

        df = gen_decile_sc_partition(cm, rng)
        part = dsc_dir / f"campaign_month={cm}"
        part.mkdir(parents=True, exist_ok=True)
        df.write_parquet(part / "decile.parquet")

    # Validation summary. Latest 3 months marked partial (still maturing).
    # has_xpm follows the XPM_AVAILABLE_FROM cutover, NOT a generic
    # "latest 2 months" rule — this matches the real-world reason XPM goes
    # missing (SAS pipeline gap, not response window immaturity).
    logs_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, cm in enumerate(months):
        is_recent = i >= len(months) - 3
        has_xpm = cm >= XPM_AVAILABLE_FROM
        rows.append({
            "campaign_month": cm,
            "partition_path": str(mart_dir / f"campaign_month={cm}"),
            "row_count": 7000,
            "total_volume": 22_000_000,
            "total_responders": 270_000,
            "total_boards": 190_000,
            "total_expected_responses": 320_000,
            "total_expected_responses_xpm": (270_000 if has_xpm else 0),
            "has_xpm": str(has_xpm).lower(),
            "sas_run_date": datetime.utcnow().isoformat(),
            "maturity_status": "partial" if is_recent else "full",
        })
    pl.DataFrame(rows).write_csv(logs_dir / "validation_summary.csv")

    print(f"Done. Rollup → {mart_dir}, decile_port → {dport_dir}, "
          f"decile_sc → {dsc_dir}, validation → {logs_dir}.")
    print("This data is SYNTHETIC. Do not use for any real analysis or share externally.")


if __name__ == "__main__":
    main()
