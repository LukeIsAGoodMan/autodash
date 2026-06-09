"""One-shot backfill: kick off SAS for [start, end], then ingest everything.

Usage:
    python scripts/run_initial_backfill.py --start 2025-01 --end 2026-04

If SAS has already been run separately and the CSVs are sitting in the export
folder, pass --skip-sas to only run ingest.

Exit code is non-zero if any step fails (SAS ERROR, missing CSVs, ingest 0).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest_rollups import run_ingest
from src.sas_runner import SASError, run_sas_pipeline
from src.utils import iso_to_campaign_month, load_config, setup_logging


def _months_inclusive(start_iso: str, end_iso: str) -> list[str]:
    s = iso_to_campaign_month(start_iso)
    e = iso_to_campaign_month(end_iso)
    out = []
    y, m = s.year, s.month
    while (y, m) <= (e.year, e.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y += 1
            m = 1
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True, help="first campaign month, e.g. 2025-01")
    p.add_argument("--end", required=True, help="last campaign month, e.g. 2026-04")
    p.add_argument("--skip-sas", action="store_true",
                   help="don't connect to SAS; just ingest whatever CSVs are present")
    p.add_argument("--force", action="store_true",
                   help="re-ingest every month even if the partition is up to date "
                        "(use after changing ingest logic, e.g. vs_band normalization)")
    args = p.parse_args()

    cfg = load_config()
    log = setup_logging(cfg)

    months = _months_inclusive(args.start, args.end)
    csv_dir = Path(cfg["paths"]["rollup_csv_dir"])

    log.info("=" * 70)
    log.info("Initial backfill")
    log.info("  range            = %s .. %s (%d months)", args.start, args.end, len(months))
    log.info("  rollup_csv_dir   = %s (exists=%s)", csv_dir, csv_dir.exists())
    log.info("  sas_export_dir   = %s", cfg["paths"]["sas_export_dir"])
    log.info("  &folder. (SAS)   = %s", cfg["sas"]["export_folder_macrovar"])
    log.info("  mart_dir         = %s", cfg["paths"]["mart_dir"])
    log.info("  Expected CSVs    :")
    for m in months:
        cm = iso_to_campaign_month(m)
        log.info("    %s -> %s", m, csv_dir / cm.rollup_filename)
    log.info("=" * 70)

    # ---- 1. SAS step --------------------------------------------------
    if not args.skip_sas:
        log.info("Phase 1: SAS pull %s -> %s", args.start, args.end)
        try:
            run_sas_pipeline(args.start, args.end, cfg)
        except SASError as e:
            log.error("SAS step failed: %s", e)
            return 2
        except Exception as e:  # noqa: BLE001
            log.exception("SAS step crashed: %s", e)
            return 2
    else:
        log.info("Phase 1 skipped (--skip-sas)")

    # ---- 2. Ingest ----------------------------------------------------
    log.info("Phase 2: ingest rollup CSVs into parquet mart (force=%s)", args.force)
    summary = run_ingest(cfg, expected_months=months, force=args.force)
    log.info("Backfill summary: %s", summary)

    successes = summary.get("added", 0) + summary.get("replaced", 0)
    if successes == 0:
        log.error(
            "Ingest produced 0 successful loads. Either the SAS export did not "
            "run, or it wrote files to a different folder. "
            "rollup_csv_dir=%s",
            csv_dir,
        )
        return 4
    if summary.get("missing_csv"):
        log.error("These months were not loaded: %s", summary["missing_csv"])
        return 5
    if summary.get("failed", 0):
        log.error("%d month(s) failed validation. Failing backfill.",
                  summary["failed"])
        return 6
    log.info("Backfill OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
