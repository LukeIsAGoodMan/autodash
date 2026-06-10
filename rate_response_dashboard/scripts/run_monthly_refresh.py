"""Monthly refresh entrypoint, intended for Windows Task Scheduler.

Default behavior: refresh the most recent N months (config.refresh.recent_refresh_window_months).
Override with --month YYYY-MM to refresh a single month.

Exit code is non-zero if any step fails:
  - SAS log contains ERROR
  - expected exp_<MMMYY>_rollup.csv not produced
  - ingest produced 0 successful loads
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.ingest_decile import run_decile_ingest
from src.ingest_rollups import run_ingest
from src.sas_runner import SASError, open_sas
from src.utils import CampaignMonth, iso_to_campaign_month, load_config, setup_logging


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


def _expected_csv_paths(months: list[str], csv_dir: Path) -> dict[str, Path]:
    return {m: csv_dir / iso_to_campaign_month(m).rollup_filename for m in months}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--month", help="single month to refresh, e.g. 2026-05")
    p.add_argument("--skip-sas", action="store_true")
    args = p.parse_args()

    cfg = load_config()
    log = setup_logging(cfg)

    if args.month:
        months = [args.month]
    else:
        n = cfg["refresh"]["recent_refresh_window_months"]
        months = _recent_months(date.today(), n)

    csv_dir = Path(cfg["paths"]["rollup_csv_dir"])

    # ---- debug header --------------------------------------------------
    log.info("=" * 70)
    log.info("Monthly refresh")
    log.info("  rollup_csv_dir   = %s (exists=%s)", csv_dir, csv_dir.exists())
    log.info("  sas_export_dir   = %s", cfg["paths"]["sas_export_dir"])
    log.info("  &folder. (SAS)   = %s", cfg["sas"]["export_folder_macrovar"])
    log.info("  mart_dir         = %s", cfg["paths"]["mart_dir"])
    log.info("  Months to refresh: %s", months)
    for m in months:
        cm = iso_to_campaign_month(m)
        log.info("    %s -> sas_reportdate=%s sas_label=%s table=%s csv=%s",
                 m, cm.sas_reportdate, cm.sas_label,
                 cm.rollup_table_name, cm.rollup_filename)
    log.info("=" * 70)

    expected = _expected_csv_paths(months, csv_dir)

    # ---- 1. SAS step ---------------------------------------------------
    if not args.skip_sas:
        try:
            with open_sas(cfg) as r:
                for m in months:
                    r.run_one_month_sas(m)
        except SASError as e:
            log.error("SAS step failed: %s", e)
            return 2
        except Exception as e:  # noqa: BLE001
            log.exception("SAS step crashed: %s", e)
            return 2
    else:
        log.info("SAS step skipped (--skip-sas)")

    # ---- 2. Verify expected CSVs landed on disk -----------------------
    log.info("Checking expected CSV files exist:")
    missing_csv = []
    for m, path in expected.items():
        ok = path.exists()
        log.info("  %s  %s", "[ok]  " if ok else "[MISS]", path)
        if not ok:
            missing_csv.append(m)
    if missing_csv:
        log.error(
            "Expected rollup CSV(s) missing: %s. "
            "Review the latest SAS log under %s/sas_*.log to see why SAS did not export.",
            missing_csv, cfg["paths"]["logs_dir"],
        )
        return 3

    # ---- 3. Ingest -----------------------------------------------------
    summary = run_ingest(cfg, expected_months=months)
    log.info("Monthly refresh summary: %s", summary)

    # Decile mart is best-effort: silently skip if SAS hasn't started
    # emitting decile CSVs for these months yet.
    decile_summary = run_decile_ingest(cfg)
    log.info("Decile ingest summary: %s", decile_summary)

    successes = summary.get("added", 0) + summary.get("replaced", 0)
    if successes == 0:
        log.error("Ingest produced 0 successful loads. Failing refresh.")
        return 4
    if summary.get("missing_csv"):
        log.error("These months were not loaded: %s", summary["missing_csv"])
        return 5
    if summary.get("failed", 0):
        log.error("%d month(s) failed validation. Failing refresh.",
                  summary["failed"])
        return 6
    log.info("Monthly refresh OK.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
