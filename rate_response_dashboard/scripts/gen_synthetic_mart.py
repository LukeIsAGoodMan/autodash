"""Generate synthetic parquet marts so the AI report pipeline can be
smoke-tested locally without company data.

NOT for any real analysis. Numbers are random with a mild trend so the
report visually shows movement, but they are NOT representative of real
campaigns. The output paths live under data/ which is gitignored, so this
fixture never enters version control.

Run from project root:

    python scripts/gen_synthetic_mart.py

Then start the dashboard or run the renderer smoke test.
"""
from __future__ import annotations

import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.utils import load_config


# Cardinality of each dim. Pick values consistent with the real catalog so
# downstream filters and joins behave the same.
DIMS = {
    "vs_band": ["530-549", "550-600", "601-639", "640-700", "701-730", "731-800"],
    "scorecard": [11, 12, 13, 14, 15],
    "Prospect_type": ["Prospecting", "Closed Remarket", "Charge-Off", "PQ Decline", "Remail"],
    "rm_flag": ["Y", "N"],
    "trm10_tier": list(range(1, 41)),
    "annual_fee": ["$75/$99", "$95/$95", "$0/$0", "$39/$39"],
    "times_mailed_12mo_cnt": list(range(0, 6)),
    "pqabandon_flag": ["Y", "N"],
    "prchargeoff_flag": ["Y", "N"],
    "prclosure_flag": ["Y", "N"],
    "prdecline_flag": ["Y", "N"],
}


def gen_rollup_partition(cm_iso: str, rng: random.Random) -> pl.DataFrame:
    """One partition (~5k rows) covering a tractable subset of the dim cross."""
    rows = []
    # Keep it modest: full cross would be huge. Sample 5k cells.
    n = 5_000
    for _ in range(n):
        # Skew volume by product so $75/$99 dominates, matching reality.
        product = rng.choices(DIMS["annual_fee"], weights=[0.50, 0.25, 0.20, 0.05])[0]
        vol = max(50, int(rng.lognormvariate(7.5, 1.0)))
        # Baseline NRR by product with light month-to-month drift.
        base_nrr = {"$75/$99": 0.012, "$95/$95": 0.009, "$0/$0": 0.007, "$39/$39": 0.015}[product]
        drift = rng.uniform(-0.001, 0.001)
        nrr = max(0.001, base_nrr + drift)
        responders = int(vol * nrr * rng.uniform(0.85, 1.15))
        boards = int(responders * rng.uniform(0.80, 0.95))
        # Expected responses calibrated near actual; A/E ratio drifts ~5%.
        exp_t = responders * rng.uniform(0.92, 1.08)
        exp_x = responders * rng.uniform(0.95, 1.05)
        rows.append({
            "campaign_month": cm_iso,
            "Prospect_type": rng.choice(DIMS["Prospect_type"]),
            "pqabandon_flag": rng.choice(DIMS["pqabandon_flag"]),
            "prchargeoff_flag": rng.choice(DIMS["prchargeoff_flag"]),
            "prclosure_flag": rng.choice(DIMS["prclosure_flag"]),
            "prdecline_flag": rng.choice(DIMS["prdecline_flag"]),
            "vs_band": rng.choice(DIMS["vs_band"]),
            "annual_fee": product,
            "times_mailed_12mo_cnt": rng.choice(DIMS["times_mailed_12mo_cnt"]),
            "trm10_tier": rng.choice(DIMS["trm10_tier"]),
            "scorecard": rng.choice(DIMS["scorecard"]),
            "rm_flag": rng.choice(DIMS["rm_flag"]),
            "volume": float(vol),
            "responders": float(responders),
            "Boards": float(boards),
            "expected_responses": float(exp_t),
            "expected_responses_xpm": float(exp_x),
        })
    return pl.DataFrame(rows)


def gen_decile_port_partition(cm_iso: str, rng: random.Random) -> pl.DataFrame:
    """20 decile bins, monotonic NRR by decile (with small noise) plus drift."""
    rows = []
    total_vol = 1_000_000
    per_dec = total_vol / 20
    # Decile 1 highest score: NRR around 2.5%, decile 20 around 0.3%.
    for d in range(1, 21):
        target_rr = 0.025 * (1.0 - (d - 1) / 19) ** 1.3 + 0.003
        target_rr *= rng.uniform(0.9, 1.1)
        vol = per_dec * rng.uniform(0.95, 1.05)
        resp = int(vol * target_rr)
        boards = int(resp * rng.uniform(0.82, 0.92))
        rows.append({
            "campaign_month": cm_iso,
            "decile": d,
            "volume": float(vol),
            "responders": float(resp),
            "Boards": float(boards),
        })
    return pl.DataFrame(rows)


def gen_decile_sc_partition(cm_iso: str, rng: random.Random) -> pl.DataFrame:
    """For each scorecard, 10 decile bins. Same monotone pattern."""
    rows = []
    for sc in DIMS["scorecard"]:
        total = 200_000
        per_dec = total / 10
        for d in range(1, 11):
            target_rr = 0.022 * (1.0 - (d - 1) / 9) ** 1.3 + 0.004
            target_rr *= rng.uniform(0.85, 1.15)
            vol = per_dec * rng.uniform(0.9, 1.1)
            resp = int(vol * target_rr)
            boards = int(resp * rng.uniform(0.82, 0.92))
            rows.append({
                "campaign_month": cm_iso,
                "scorecard": sc,
                "decile": d,
                "volume": float(vol),
                "responders": float(resp),
                "Boards": float(boards),
            })
    return pl.DataFrame(rows)


def _months(n: int) -> list[str]:
    """Most recent n months ISO, oldest first. Calendar-correct (no day-math hacks)."""
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
    rng = random.Random(20260610)  # deterministic so re-runs are stable

    mart_dir = Path(cfg["paths"]["mart_dir"])
    dport_dir = Path(cfg["paths"]["decile_port_mart_dir"])
    dsc_dir = Path(cfg["paths"]["decile_sc_mart_dir"])
    logs_dir = Path(cfg["paths"]["logs_dir"])

    months = _months(15)
    print(f"Generating {len(months)} months: {months[0]} → {months[-1]}")

    for cm in months:
        # Rollup mart
        df = gen_rollup_partition(cm, rng)
        part = mart_dir / f"campaign_month={cm}"
        part.mkdir(parents=True, exist_ok=True)
        df.write_parquet(part / "rollup.parquet")

        # Portfolio decile
        df = gen_decile_port_partition(cm, rng)
        part = dport_dir / f"campaign_month={cm}"
        part.mkdir(parents=True, exist_ok=True)
        df.write_parquet(part / "decile.parquet")

        # Scorecard decile
        df = gen_decile_sc_partition(cm, rng)
        part = dsc_dir / f"campaign_month={cm}"
        part.mkdir(parents=True, exist_ok=True)
        df.write_parquet(part / "decile.parquet")

    # Validation summary — mark the 3 most recent months as partial to exercise
    # the maturity UI path. Drop XPM for the latest 2 months.
    logs_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for i, cm in enumerate(months):
        is_recent = i >= len(months) - 3
        has_xpm = i < len(months) - 2
        rows.append({
            "campaign_month": cm,
            "partition_path": str(mart_dir / f"campaign_month={cm}"),
            "row_count": 5000,
            "total_volume": 100_000_000,
            "total_responders": 1_200_000,
            "total_boards": 1_000_000,
            "total_expected_responses": 1_200_000,
            "total_expected_responses_xpm": 1_200_000 if has_xpm else 0,
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
