# Rate Response Dashboard

Hybrid Python + SAS pipeline that turns the monthly mail-file response / board
rollup into a Dash-based analytics dashboard. SAS does the data engineering
against the mailfile and Oracle CAPS database; Python orchestrates SAS,
ingests SAS-exported aggregate CSVs into parquet marts, validates, and serves
a six-tab dashboard.

**The dashboard never reads customer-level data.** It reads only the
aggregated rollups SAS emits.

---

## What's inside

```
┌─────────────────────────────────────────────────────────────────────────┐
│  SAS (trusted data engine on Unix box)                                  │
│   readmailfile_trm → getresponse_trm → finalresponse_trm                │
│                          ↓                                              │
│   assign_psi_tier → rank_deciles (proc rank, equal-volume bins)         │
│                          ↓                                              │
│   rollup + proc export  →  exp_<MMMYY>_rollup.csv      (main rollup)    │
│   rollup_decile_sc      →  exp_<MMMYY>_decile_sc.csv   (P4: 10/sc)      │
│   rollup_decile_port    →  exp_<MMMYY>_decile_port.csv (P4: 20 deciles) │
└─────────────────────────────────────────────────────────────────────────┘
                          ↓ UNC mount
┌─────────────────────────────────────────────────────────────────────────┐
│  Python (on the Windows box where dashboard runs)                       │
│   ingest_rollups → data/mart/rate_response_rollup/                      │
│   ingest_decile  → data/mart/decile_sc/  +  data/mart/decile_port/      │
│   validation     → data/logs/load_log.csv + validation_summary.csv      │
│                                                                         │
│   Dash app (six tabs)                                                   │
│   Executive | Pivot | Model Performance | Rank Order | DQ | Export      │
└─────────────────────────────────────────────────────────────────────────┘
```

## How to run

### 0. One-time setup

```
pip install -r requirements.txt
```

`C1BConnections` / `Agora` is the company SAS connector — **not** on pip;
it lives at `C:\Users\<user>\88c6afda…\RFS-MAP\SAS-DATA-PULL\` and is
imported lazily inside `src/sas_runner.py`.

### 1. Initial backfill (first time, or after a deploy to a fresh folder)

```
python scripts/run_initial_backfill.py --start 2025-01 --end 2026-05
```

This runs `%run_months(start=01JAN2025, end=01MAY2026)` in SAS, then ingests
every `exp_*_rollup.csv` and `exp_*_decile_*.csv` it finds.

If SAS has already produced CSVs and you only want to (re)build the parquet
marts:

```
python scripts/run_initial_backfill.py --start 2025-01 --end 2026-05 --skip-sas
```

Add `--force` to overwrite all existing partitions (useful after changing
ingest logic, e.g. a new column type):

```
python scripts/run_initial_backfill.py --start 2025-01 --end 2026-05 --skip-sas --force
```

### 2. Monthly refresh

```
python scripts/run_monthly_refresh.py
```

Default behavior refreshes the most recent N months
(`refresh.recent_refresh_window_months`, default 3) because responders and
boards keep arriving from the offer drop. Schedule monthly in Windows
Task Scheduler.

Single-month rerun:

```
python scripts/run_monthly_refresh.py --month 2026-05
```

Both scripts exit non-zero on any failure (SAS ERROR, missing CSV, validation
failure, etc.), so they're safe to wrap in scheduler error alerts.

### 3. Dashboard

```
python scripts/run_dashboard.py
```

Default `http://0.0.0.0:8050`. Bind address / port / debug via env vars
`DASH_HOST`, `DASH_PORT`, `DASH_DEBUG`. For production-style deployment
(survive logoff, multi-user) wrap with **waitress** + **NSSM**; the Flask
dev server `python scripts/run_dashboard.py` ships with is fine for one
user at a time.

---

## Dashboard tabs

| Tab | Headline content |
|---|---|
| **Executive Summary** | KPI cards (Latest month + All months × 8 metrics) with a dynamic month-of-interest chip; trend chart over `default_lookback_months` (default 24 months) |
| **Pivot View** | Pivot table with inline volume bars or MoM Δ color mode (intensity scales with `\|delta\|`); Overall row + Overall column; stacked-bar + line combo chart |
| **Model Performance** | TRM expected-vs-actual by vs_band (all months) + XPM expected-vs-actual by vs_band (auto-filtered to months that have XPM); monthly trend with three lines (actual / expected_trm / expected_xpm, xpm gap-aware) |
| **Rank Order** (P4) | KS / AUC / Gini / Top decile lift + 4 business KPIs; cumulative capture curve with 45° reference; response rate by decile bar; KS-over-time trend; decile detail table with **per-decile KS** + **Misrank** ⚠ flag. Scorecard filter routes: All → portfolio mart (20 deciles); 1–4 → scorecard mart (10 deciles per scorecard) |
| **Data Quality** | Per-partition table: row count, sums, `has_xpm`, `sas_run_date`, **`maturity_status`** |
| **Export** | CSV / Excel download of the currently filtered + aggregated mart |
| **AI Report** | One-click monthly narrative report (HTML). **Month picker** lets the analyst back-date the report to any month in the mart (defaults to latest); a colored chip beside the dropdown flags `partial` / `unknown` maturity *before* the LLM spend. Six analytical sections (Headline KPIs → Population mix → Big Mac cohort → Top combinations → Model catch → TRM vs XPM). Optional LLM commentary with `material_movers` (green Focus chips) + `noise_flags` (gray Noise chips). Loading overlay while generating; per-slot fallback explains why if the LLM step fails. |

### Filters (consistent across Pivot / Model / Export)

- **Campaign months** — pick a contiguous range via `From / To` single-selects,
  OR cherry-pick a non-contiguous set via the multi-select dropdown
  (multi-select wins over From/To)
- 7 dimension multi-selects: `vs_band`, `scorecard`, `Prospect_type`,
  `rm_flag`, `trm10_tier`, `annual_fee`, `times_mailed_12mo_cnt`
- When all month inputs are empty, the dashboard restricts to the most recent
  `default_lookback_months` (default 24)

---

## AI Report Generator

A second, narrative-first surface that lives in the AI Report tab. Reads
the same rollup + decile marts as the rest of the dashboard, runs a
deterministic 6-section pipeline (no LLM required), and optionally adds
LLM-written commentary under each chart.

### Pipeline

```
read_mart ─┐
           ├─→ snapshot_builder ─→ mom_yoy ─→ mix_analysis ─┐
read_decile┘                                                 │
                                                             ↓
                                            slice_trends / big_mac /
                                            combinations / model_catch /
                                            model_compare
                                                             ↓
                                                       ReportPackage
                                                             ↓
                          chart_builder (stacked-bar volume + overlay rate line)
                                                             ↓
                          [optional] LLM commentary writer (per section)
                                                             ↓
                          renderer (Jinja2 → self-contained HTML)
                                                             ↓
                       reports/<timestamp>/report.html  +  iframe preview
```

### Picking the report month

The dropdown next to the Generate button is populated from
`validation_summary.csv` whenever the AI Report tab is activated, newest
month first. Each option carries its maturity in parentheses
(`2026-06 (partial)`), and a colored chip beside the dropdown spells out
the consequence:

| Chip | Meaning |
|---|---|
| `[full · XPM on/off]` (green) | Safe to publish; XPM availability shown for the picked month |
| `[partial — NRR still maturing, treat report as preliminary]` (red) | Boards/responders for this month are still arriving from the offer drop; commentary will prepend `Preliminary:` automatically |
| `[unknown — no validation_summary entry]` (amber) | The pipeline could not determine maturity — usually means SAS rerun date is missing |

`build_report_package(cfg, target_month="2026-05")` truncates the rollup
mart, decile mart, and maturity dict to `<= target_month` before any
analysis runs, so MoM/YoY anchors, lookback windows, and trend charts
all treat the picked month as the latest. When `target_month` is
omitted (CLI, headless), behavior is unchanged: the latest month in the
mart is used.

### Commentary slots

Each commentary slot the LLM populates carries:

- `headline` — one declarative sentence
- `body` — 40–120 words, no causal language unless backed by a PAF event (Stage 3)
- `material_movers` — the analyst's focus list (rendered as green chips)
- `noise_flags` — movers to demote (rendered as gray chips)

The LLM is forced to commit to a materiality call rather than just
restating the chart.

### Configuration

Add to `config/config.yaml`:

```yaml
ai_agent:
  llm:
    provider: stub          # stub | openai | gemini
    model: gpt-4o
    api_key_env: OPENAI_API_KEY
    timeout_seconds: 30
    enabled: false          # flip to true when ready; defaults off
    audit_enabled: true     # Sprint B audit pass; set false to save spend
  chart_lookback_months: 15
  slice_dims: [annual_fee, vs_band, scorecard, Prospect_type, rm_flag, times_mailed_12mo_cnt]
  big_mac:
    Prospect_type: "Prospecting"
    rm_flag: 0
    times_mailed_12mo_cnt: 0
    trm10_tier_in: [1, 21]
    drill_dim: "vs_band"
  combinations:
    dim_pairs:
      - [annual_fee, vs_band]
      - [annual_fee, Prospect_type]
      # ...
    min_combo_volume: 5000
    top_k: 8
```

Environment overrides (handy for switching providers without editing config):

```
AI_REPORT_LLM_PROVIDER=openai      # or gemini, stub
AI_REPORT_LLM_MODEL=gpt-4o
OPENAI_API_KEY=sk-...              # name comes from api_key_env
```

### Graceful failure

The LLM step **never raises out of the pipeline**. If anything goes
wrong, the affected commentary slot is filled with a fallback whose
`headline` and `body` name the reason:

| Failure mode | What you see |
|---|---|
| API key env var unset | Every slot's headline: `Commentary unavailable — Environment variable OPENAI_API_KEY is not set.` |
| One section's API call fails | Only that section's slots show the fallback; other sections render real commentary |
| LLM returns zero slots | That section's expected slots filled with `"LLM returned an empty slots list"` |
| LLM omits an expected slot | Missing slot filled with `"LLM omitted this slot from its response"` |

A gray "Noise" chip with the reason also appears under the fallback
headline so the reader spots dead commentary at a glance.

### Provider status

| Provider | State | Use case |
|---|---|---|
| `stub` | Working — returns a canned non-LLM response, safe default | Offline / CI |
| `openai` | Working — `gpt-4o` via `chat.completions.parse` with strict structured output | Mac dev (peiyaohe2's key) |
| `gemini` | **Placeholder — raises on construct** | Needs to be wired up before Windows production validation. Interface (`generate_structured`) and schema (`SectionCommentary`) are provider-agnostic, so this is purely an SDK-binding job inside `src/ai_agent/llm/gemini_client.py` |

---

## Metrics

### Main rollup mart (per month × dim × dim × ...)
All rates **recomputed from sums** at every aggregation — never averaged:

| Metric | Formula |
|---|---|
| `actual_response_rate` | sum(responders) / sum(volume) |
| `actual_board_rate` | sum(Boards) / sum(volume) |
| `expected_rr_trm` | sum(expected_responses) / sum(volume) |
| `expected_rr_xpm` | sum(expected_responses_xpm) / sum(volume) — null when no xpm-bearing rows are in the group |
| `actual_vs_expected_trm/xpm` | actual_response_rate / expected_rr_* |

### Decile marts (P4, per month × decile × [scorecard])

| Metric | Formula |
|---|---|
| `response_rate` per decile | responders / volume in bin |
| `cum_capture` | cumsum(responders) / total_responders |
| `cum_volume_pct` | cumsum(volume) / total_volume |
| `cum_non_resp_pct` | cumsum(non_responders) / total_non_responders |
| `lift` | response_rate / overall_response_rate |
| `per_decile_ks` | \|cum_capture − cum_non_resp_pct\| at each decile |
| `misrank` | 1 if response_rate > previous decile's, else 0 |
| `KS` (table-wide) | max(per_decile_ks) |
| `AUC` | trapezoidal integral of ROC(x=cum_non_resp_pct, y=cum_capture) |
| `Gini` | 2 × AUC − 1 |

---

## Data maturity (full vs partial)

A campaign month's responders and boards keep arriving from the offer drop
for ~3 months. `validation.py` marks each partition `partial` until SAS was
rerun at least `mart.maturity_threshold_months` (default 3) after the
campaign month ended; from then on it's `full`.

The "SAS rerun date" is taken from the latest `source_modified_time` in
`load_log.csv` (the CSV mtime SAS wrote, not the parquet mtime — avoids
`--skip-sas --force` reingests being mis-counted as refreshes).

Surfaces:
- **Hero**: a chip after the latest month — `[partial]` (amber) or `[full]` (green)
- **Data Quality tab**: `sas_run_date` + `maturity_status` columns, colored

Tune by editing `mart.maturity_threshold_months` in `config/config.yaml`.

---

## Ingest decision logic

`src/ingest_rollups.plan_ingestion` walks the SAS CSV folder and decides per
file:

| Condition | Action |
|---|---|
| `--force` and partition exists | `replace` |
| Partition missing | `add` |
| CSV mtime newer than partition mtime | `replace` |
| Month within `recent_refresh_window_months` | `replace` (forced) |
| Otherwise | `skip` |
| Month older than `history_freeze_months` | `skip` (frozen) |

Same logic in `ingest_decile.py` for the two decile streams.

### No append path, ever

The only writer is `build_mart.write_partition_safely`:

1. Write to `campaign_month=YYYY-MM__tmp/rollup.parquet`
2. Validate
3. Remove existing `campaign_month=YYYY-MM/` if any
4. Rename `__tmp` → final

A month overwriting itself is a no-op for downstream sums; double-count is
structurally impossible.

---

## How to validate against the old Excel pivot

1. Pick a single month in both Excel and the dashboard.
2. Compare grand totals on the Executive tab to Excel's grand total cell.
3. Compare a non-trivial slice on the Pivot tab (row=`vs_band`,
   metric=`actual_response_rate`) against the same in Excel.
   If Excel divergence is exactly the order of magnitude of averaging-vs-
   summing, Excel was averaging rates — the dashboard never does that.
4. Use Export tab → download CSV → SUM/SUM yourself in Excel for spot checks.

---

## Repo layout

```
config/config.yaml
sas/rate_response_pipeline.sas
src/
  sas_runner.py        # C1BConnections wrapper, fail-fast on SAS ERROR
  ingest_rollups.py    # main rollup ingest
  ingest_decile.py     # P4 decile ingest (sc + port)
  build_mart.py        # parquet writer + reader with dtype harmonization
  validation.py        # validation_summary + maturity_status
  metrics.py           # rates + KS/AUC/Gini/misrank
  utils.py             # config, logging, CampaignMonth, filename parsers
dashboard/
  app.py               # Dash app entrypoint
  layout.py            # 6 tabs, hero, sticky filter bar
  callbacks.py         # all interactivity
  plotly_template.py   # omni_light template (default for all figures)
  assets/custom.css    # blue/white theme, auto-loaded by Dash
scripts/
  run_initial_backfill.py
  run_monthly_refresh.py
  run_dashboard.py
data/                  # gitignored
  mart/rate_response_rollup/  campaign_month=YYYY-MM/rollup.parquet
  mart/decile_sc/             campaign_month=YYYY-MM/decile.parquet
  mart/decile_port/           campaign_month=YYYY-MM/decile.parquet
  rollup_csv/                 # local copy if you keep one; UNC by default
  logs/                       load_log.csv, validation_summary.csv, sas_*.log, app.log
tests/test_metrics.py  # protects the no-average rule + KS/AUC/Gini math
```

---

## SAS-side history

The original `saspull2.0.ipynb` had three latent bugs we discovered during
P0–P4. All are **resolved** in `sas/rate_response_pipeline.sas`:

| Original issue | Status |
|---|---|
| Nested `/* … /* … */ … */` comments breaking macro compile | Resolved — banner comments rewritten, no nesting |
| `%rollup` reading `trm.&ds._finalresponse` BEFORE `%run_one_month` writes it | Resolved — table assignment moved before `%rollup` call |
| `expected_responses_xpm = sum(EXP_RESPONSE_SCORE)` but field missing from `keep=` | Resolved — `EXP_RESPONSE_SCORE` added to `keep=`, coerced to numeric with `*1` (mirrors the existing `TRM_Score = TRM10_Score*1` pattern) |

P4 additions (additive, do not change existing behavior):
- `%rank_deciles` — proc rank by TRM_Score: `sc_decile` 1..10 within
  scorecard, `port_decile` 1..20 across portfolio
- `%rollup_decile_sc` / `%rollup_decile_port` — two new compact CSV exports
  driving the Rank Order tab

---

## Tests

```
pytest tests/
```

`test_metrics.py` covers:
- Rates are `sum(num) / sum(den)` — explicitly diverges from `mean(rates)`
- XPM null aggregation (no xpm-bearing rows → null, not 0)
- KS = max(per-decile KS) and is null when no responders
- AUC > 0.7 on strongly rank-ordered data, ≈ 0.5 on flat data
- Gini = 2×AUC − 1
- Misrank = 0 on monotone data, > 0 on a swapped-decile fixture
- Cell suppression keeps counts but nulls rates

---

## Implementation roadmap (status)

### Dashboard track

- ✅ **Phase 1** — Python scan + parquet mart (`ingest_rollups`, `build_mart`)
- ✅ **Phase 2** — Validation + reconciliation (`validation.py`, load_log, validation_summary)
- ✅ **Phase 3** — Dash dashboard (6 tabs, blue/white theme, sticky filter bar, MoM color, range filters)
- ✅ **Phase 4** — Decile mart + Rank Order tab + KS / AUC / Gini / Misrank + maturity status
- ⬜ **Phase 5** — Production deployment (waitress + NSSM as Windows Service); email alerts on `status==failed` rows in load_log.csv
- ⬜ **Phase 6** — Optional: Airflow / Control-M integration if the schedule grows beyond Windows Task Scheduler

### AI agent track

- ✅ **Stage 1** — Deterministic 6-section pipeline + chart builder + Jinja2
  HTML renderer + AI Report tab with iframe preview
- ✅ **Stage 2** — LLM commentary: provider-agnostic factory (stub / openai /
  gemini-placeholder), Pydantic structured output, per-section prompts with
  few-shot prioritization examples, materiality chips (Focus / Noise),
  graceful per-slot fallback when the LLM step fails, loading spinner +
  client-side immediate-feedback on Generate
- 🔄 **Sprint A — Windows + Gemini production validation** *(next, immediate)*
  - Wire up `GeminiClient` against the company Gemini Enterprise SDK
    (interface contract already in `client.py`)
  - Run the full pipeline against the real production mart on the Windows
    box; surface and fix any dim-shape edge cases (unseen `scorecard`
    values, empty per-dim slices, XPM gaps on different months)
  - Confirm the loading overlay + fallback path behave correctly when
    Gemini Enterprise is or is not reachable
- ✅ **Sprint B — Cross-section synthesis + audit pass**
  - **Dispatch reorder**: builder signature extended with optional
    `prior_slots: dict`; section_b_summary now runs AFTER every per-dim
    `B-{dim}` builder and reads each dim's headline + materiality call to
    synthesize across dims ("annual_fee dominates; rm_flag dim is noise")
    instead of re-deriving the ranking from raw mix shifts.
  - **Audit pass** (`src/ai_agent/llm/auditor.py`): same LLM endpoint,
    fresh `_AUDIT_SYSTEM` prompt that frames the model as an INDEPENDENT
    QA reviewer. One audit call per section. Checks: unit bugs (bps vs
    pp), hallucinated numbers (not in facts), missing caveats (partial
    maturity, XPM unavailable), causal language without PAF backing,
    internal contradictions, missing prioritization on ranking slots.
  - Findings keyed by section letter (A–F) and rendered as colored
    banners under the section H2 (red = error, amber = warning, blue =
    info). Audit failure itself becomes an info banner so the report
    never breaks. Disable via `ai_agent.llm.audit_enabled: false`.
- ⬜ **Stage 3 — PAF integration** *(2–3 weeks)*
  - LLM-based PDF extraction agent reads the analyst-maintained PAF
    documents and emits structured `PAFEvent` records (event type, affected
    dim, effective date range, expected direction)
  - Time-window + dim join against `ReportPackage` movers
  - Prompt upgrade: when a mover matches a PAFEvent, **unlock** causal
    language ("X coincides with the credit policy change on 2026-04
    affecting this segment") and require a citation to the PAFEvent id
- ⬜ **Stage 4 — Polish for monthly cadence** *(after PAF lands)*
  - Decile-drift / segment-migration view from the so-far-unused
    `decile_sc` mart
  - Email delivery + archive of the monthly HTML
  - Optional per-product report variants
