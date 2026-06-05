"""One-shot backfill: kick off SAS for [start, end], then ingest everything.

Usage:
    python scripts/run_initial_backfill.py --start 2025-01 --end 2026-04

If you've already run the SAS side manually (the notebook way) and the CSVs
are sitting in the export folder, pass --skip-sas to only run ingest.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest_rollups import run_ingest
from src.sas_runner import run_sas_pipeline
from src.utils import load_config, setup_logging


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--start", required=True, help="first campaign month, e.g. 2025-01")
    p.add_argument("--end", required=True, help="last campaign month, e.g. 2026-04")
    p.add_argument("--skip-sas", action="store_true",
                   help="don't connect to SAS; just ingest whatever CSVs are present")
    args = p.parse_args()

    cfg = load_config()
    log = setup_logging(cfg)

    if not args.skip_sas:
        log.info("Phase 1: SAS pull %s → %s", args.start, args.end)
        run_sas_pipeline(args.start, args.end, cfg)
    else:
        log.info("Phase 1 skipped (--skip-sas)")

    log.info("Phase 2: ingest rollup CSVs into parquet mart")
    summary = run_ingest(cfg)
    log.info("Backfill complete: %s", summary)


if __name__ == "__main__":
    main()
