# Rate Response Dashboard

Hybrid Python + SAS pipeline that turns the monthly mail-file response /
board rollup into a Dash-based pivot-and-trend dashboard. SAS does the
data engineering against the mailfile and Oracle CAPS database; Python
orchestrates SAS, ingests SAS-exported rollup CSVs into a parquet mart,
validates, and serves a dashboard.

The dashboard never reads customer-level data. It reads only aggregated
rollups.

---

## How to run

### 1. Initial backfill (first time)

```
python scripts/run_initial_backfill.py --start 2025-01 --end 2026-04
```

This:
1. Opens SAS via `C1BConnections` (exactly the way the notebook does it).
2. Submits `libname trm ...; %let folder=...; %include sas/rate_response_pipeline.sas;`.
3. Submits `%run_months(start=01JAN2025, end=01APR2026);`.
4. Scans the SAS export folder for `exp_*_rollup.csv`.
5. Validates each rollup CSV and writes one parquet partition per month.

If SAS has already been run separately and the CSVs exist on disk,
skip the SAS step:

```
python scripts/run_initial_backfill.py --start 2025-01 --end 2026-04 --skip-sas
```

### 2. Monthly refresh

```
python scripts/run_monthly_refresh.py
```

By default this refreshes the most recent N months (configured by
`refresh.recent_refresh_window_months`, default 3) because response /
board figures keep maturing. Schedule it monthly in Windows Task
Scheduler.

A single-month rerun:

```
python scripts/run_monthly_refresh.py --month 2026-06
```

### 3. Dashboard

```
python scripts/run_dashboard.py
```

Opens on `http://localhost:8050`. Bind address / port via env vars
`DASH_HOST`, `DASH_PORT`, `DASH_DEBUG`.

---

## How new and updated months are detected

`src/ingest_rollups.plan_ingestion` walks `data/rollup_csv/` and runs
this decision for every `exp_<MMMYY>_rollup.csv`:

| Condition                            | Action  |
|--------------------------------------|---------|
| partition missing on disk            | `add`   |
| csv mtime newer than partition mtime | `replace` |
| month is within recent refresh window | `replace` (forced) |
| neither of the above                 | `skip`  |
| month older than `history_freeze_months` | `skip` (frozen) |

Files older than the freeze window are never touched, even if the CSV
gets re-emitted. Bump `history_freeze_months` in config to allow it.

### How duplicate appends are avoided

There is no append path anywhere. The only write is
`build_mart.write_partition_safely`, which:

1. writes to `campaign_month=YYYY-MM__tmp/rollup.parquet`
2. validates
3. removes the existing `campaign_month=YYYY-MM/` if any
4. renames `__tmp` → final

The same month overwriting itself is a no-op for downstream sums; it
cannot double-count.

---

## How to validate against the old Excel pivot

A common QA exercise the first time you ship this:

1. Open both the old Excel pivot and this dashboard.
2. Set the dashboard filter to a single month.
3. On Excel: read total volume / responders / boards from the grand total cell.
4. On dashboard's Executive tab: read the same KPI cards.
5. They should match exactly (within rounding).

Then verify a non-trivial slice:

6. In the dashboard Pivot tab: row dim = `vs_band`, metric = `actual_response_rate`.
7. In Excel: same pivot, with `actual_response_rate = sum(responders) / sum(volume)`.
   (Old Excel pivots often averaged `GRR` instead — that's the trap this
    dashboard fixes. If numbers diverge, Excel was averaging rates.)
8. Repeat with `expected_rr_trm` and `expected_rr_xpm`.

The CSV export tab makes spot-checks easy: download a filtered CSV and
pivot it in Excel using SUM/SUM yourself, then compare.

---

## SAS-side caveats (carry over from the original notebook)

These were noted but not auto-fixed. Confirm before going live:

1. **Nested `/* ... /* ... */ ... */` comments** in `%finalresponse_trm`.
   SAS doesn't support nested block comments; behavior is brittle.
2. **`%rollup` reads `trm.&ds._finalresponse`, but `%run_one_month`
   writes that table AFTER calling `%rollup`.** First-time month runs
   will see stale or absent data. Likely fix: swap order, or change
   `%rollup` to read `WORK.&ds._finalresponse`.
3. **`expected_responses_xpm = sum(EXP_RESPONSE_SCORE)`** but
   `EXP_RESPONSE_SCORE` is not in the mailfile `keep=`. Confirm where
   it's sourced or the column will be null in every rollup.

---

## Implementation roadmap

- **Phase 1 — Python scan + parquet mart**
  - `ingest_rollups.py`, `build_mart.py`, `metrics.py`, `utils.py`
  - Run against pre-existing SAS CSVs with `--skip-sas`
- **Phase 2 — Validation + reconciliation**
  - `validation.py`, `load_log.csv`, `validation_summary.csv`
  - Compare Excel grand totals; iterate on rule thresholds
- **Phase 3 — Dash dashboard**
  - `dashboard/{app,layout,callbacks}.py`
  - Five tabs; KPI cards; pivot; model perf; DQ; export
- **Phase 4 — Scheduler / monthly automation**
  - Windows Task Scheduler invoking `run_monthly_refresh.py`
  - Email / log alerts on `status == 'failed'` rows in `load_log.csv`
  - Future: lift into Airflow / Control-M if/when needed

---

## Tests

```
pytest tests/
```

`test_metrics.py` is the load-bearing test. It encodes the rule that
rates must be `sum(num) / sum(den)` and explicitly checks that the
result differs from naively averaging the cell-level rates.
