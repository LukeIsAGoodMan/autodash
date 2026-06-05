"""Monthly refresh entrypoint, intended for Windows Task Scheduler.

Default behavior: refresh the most recent N months (config.refresh.recent_refresh_window_months).
Override with --month YYYY-MM to refresh a single month.

Usage:
    python scripts/run_monthly_refresh.py
    python scripts/run_monthly_refresh.py --month 2026-06
    python scripts/run_monthly_refresh.py --skip-sas       # ingest-only
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest_rollups import run_ingest
from src.sas_runner import open_sas
from src.utils import CampaignMonth, load_config, setup_logging


def _recent_months(today: date, n: int) -> list[str]:
    """Return last n months ending at `today`, in ISO 'YYYY-MM' form."""
    out = []
    y, m = today.year, today.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y -= 1
            m = 12
    return list(reversed(out))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--month", help="single month to refresh, e.g. 2026-06")
    p.add_argument("--skip-sas", action="store_true")
    args = p.parse_args()

    cfg = load_config()
    log = setup_logging(cfg)

    if args.month:
        months = [args.month]
    else:
        n = cfg["refresh"]["recent_refresh_window_months"]
        months = _recent_months(date.today(), n)

    log.info("Refresh targets: %s", months)

    if not args.skip_sas:
        with open_sas(cfg) as r:
            for m in months:
                r.run_one_month_sas(m)
    else:
        log.info("SAS step skipped (--skip-sas)")

    summary = run_ingest(cfg)
    log.info("Monthly refresh complete: %s", summary)


if __name__ == "__main__":
    main()
