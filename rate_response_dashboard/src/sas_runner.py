"""Thin wrapper around the company's C1BConnections / saspy submission flow.

Contract:
  - Do NOT %include any local file. SAS server may be on Unix and cannot
    read C:\\Users\\... paths. We always submit the .sas content inline.
  - Every submit saves the full LOG under data/logs/sas_<ts>_<label>.log.
  - Every submit scans the LOG for lines starting with 'ERROR' and raises
    SASError on the first hit. The pipeline stops; we do NOT ingest after
    a SAS error.
  - After priming we verify the critical macros compiled via %sysmacexist.

Three public entrypoints:
    run_sas_pipeline(start_month, end_month) -> SAS log dict
    run_one_month_sas(campaign_month)        -> SAS log dict
    pull_sas_table(table_name, libref='WORK')-> pandas.DataFrame
"""
from __future__ import annotations

import logging
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pandas as pd

from .utils import CampaignMonth, ensure_dir, iso_to_campaign_month, load_config

log = logging.getLogger(__name__)

# Match SAS error lines. Standard SAS log uses 'ERROR:' or 'ERROR <num>-<num>:'.
_ERROR_LINE = re.compile(r"^ERROR(\s+\d+-\d+)?:", re.MULTILINE)

# Macros we require for the pipeline to be runnable.
_REQUIRED_MACROS = [
    "run_one_month", "run_one_month_from_existing", "run_months", "rollup",
    "readmailfile_trm", "getresponse_trm", "finalresponse_trm",
]


class SASError(RuntimeError):
    """Raised when SAS log contains ERROR lines or required macros are missing."""


def _import_agora():
    """Lazy import so dev machines without Agora can still load the package."""
    sys.path.append(
        fr"C:\Users\{os.getlogin()}\88c6afda0b696ca552ccd7000b7ae067\RFS-MAP\SAS-DATA-PULL"
    )
    from Agora import C1BConnections  # type: ignore
    return C1BConnections


class SASRunner:
    """Wraps connect / submit / sd2df / close. Use as context manager."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.cnx = None  # C1BConnections instance
        self.pipeline_file = Path(cfg["paths"]["sas_pipeline_file"]).resolve()
        self.trm_libname = cfg["sas"]["trm_libname"]
        self.trm_libpath = cfg["sas"]["trm_libpath"]
        self.export_folder = cfg["sas"]["export_folder_macrovar"]
        self.logs_dir = Path(cfg["paths"]["logs_dir"]).resolve()
        ensure_dir(self.logs_dir)

        # Read the .sas content ONCE at construction. If the file is missing
        # we fail before opening the SAS connection.
        if not self.pipeline_file.exists():
            raise FileNotFoundError(f"SAS pipeline file not found: {self.pipeline_file}")
        self._sas_macro_text = self.pipeline_file.read_text(encoding="utf-8")
        log.info("Loaded SAS pipeline from %s (%d chars)",
                 self.pipeline_file, len(self._sas_macro_text))

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        C1BConnections = _import_agora()
        self.cnx = C1BConnections()
        self.cnx.connect_to_SAS()
        log.info("SAS connection established")
        self._prime_environment()
        self._verify_macros_compiled()

    def disconnect(self) -> None:
        if self.cnx is not None:
            try:
                self.cnx.close_connections()
            finally:
                self.cnx = None
            log.info("SAS connection closed")

    def __enter__(self) -> "SASRunner":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.disconnect()

    # ------------------------------------------------------------------ private
    def _save_log(self, log_text: str, label: str) -> Path:
        """Persist the full SAS LOG to disk and return the path."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.logs_dir / f"sas_{ts}_{label}.log"
        path.write_text(log_text or "", encoding="utf-8")
        return path

    def _print_tail(self, log_text: str, n: int = 200) -> None:
        """Print last n lines of SAS log to the Python logger (and stdout)."""
        lines = (log_text or "").splitlines()
        tail = "\n".join(lines[-n:])
        log.info("---- SAS LOG TAIL (last %d lines) ----\n%s\n---- END SAS LOG TAIL ----",
                 min(n, len(lines)), tail)

    def _check_for_errors(self, log_text: str, log_file: Path, label: str) -> None:
        """Raise SASError if the log contains any ERROR: lines.

        Prints up to CONTEXT_BEFORE lines BEFORE each ERROR and CONTEXT_AFTER
        lines AFTER, with global log line numbers, so the offending statement
        is visible without re-opening the .log file.
        """
        CONTEXT_BEFORE = 30
        CONTEXT_AFTER = 5

        lines = (log_text or "").splitlines()
        err_idx = [i for i, ln in enumerate(lines) if _ERROR_LINE.match(ln)]
        if not err_idx:
            return

        chunks = []
        for i in err_idx[:5]:  # first 5 ERRORs is plenty for diagnosis
            start = max(0, i - CONTEXT_BEFORE)
            end = min(len(lines), i + 1 + CONTEXT_AFTER)
            numbered = []
            for j in range(start, end):
                marker = ">>" if j == i else "  "
                numbered.append(f"  {marker} {j+1:5d} | {lines[j]}")
            chunks.append(
                f"--- ERROR at log line {i+1} (context {start+1}-{end}) ---\n"
                + "\n".join(numbered)
            )

        msg = (
            f"SAS submit [{label}] failed: {len(err_idx)} ERROR line(s).\n"
            + "\n\n".join(chunks)
            + f"\n\nFull log: {log_file}"
        )
        raise SASError(msg)

    def _submit(self, code: str, label: str = "submit") -> dict:
        """Submit SAS code, persist log, print tail, raise on ERROR."""
        if self.cnx is None:
            raise RuntimeError("SAS not connected. Call connect() first.")
        log.info("Submitting SAS code [%s] (%d chars)", label, len(code))
        result = self.cnx.SAS_Connection.submit(code) or {}
        log_text = result.get("LOG", "") or ""
        log_file = self._save_log(log_text, label)
        log.info("SAS log saved: %s", log_file)
        self._print_tail(log_text)
        self._check_for_errors(log_text, log_file, label)
        return result

    def _prime_environment(self) -> None:
        """Assign trm libname, set &folder, and inline the macro file content.

        We deliberately do NOT use %include here. The SAS session may be on a
        Unix server that cannot read C:\\Users\\... paths from the Python box.
        """
        prelude = (
            f"libname {self.trm_libname} '{self.trm_libpath}';\n"
            f"%let folder = {self.export_folder};\n"
        )
        # Inline the .sas content so the SAS server compiles macros from text,
        # not from a file path it cannot reach.
        self._submit(prelude + self._sas_macro_text, label="prime")
        log.info("SAS environment primed (libname + macro defs inlined)")

    def _verify_macros_compiled(self) -> None:
        """Probe %sysmacexist for the macros we will call."""
        puts = "\n".join(
            f"%put MACRO_CHECK {m}=%sysmacexist({m});" for m in _REQUIRED_MACROS
        )
        result = self._submit(
            f"%put MACRO_CHECK_BEGIN;\n{puts}\n%put MACRO_CHECK_END;",
            label="macro_check",
        )
        log_text = result.get("LOG", "") or ""
        missing = []
        for m in _REQUIRED_MACROS:
            if f"MACRO_CHECK {m}=1" not in log_text:
                missing.append(m)
        if missing:
            raise SASError(
                f"Required SAS macros not compiled: {missing}. "
                f"This usually means the macro defs in {self.pipeline_file} have "
                f"a SAS syntax error. Check the latest data/logs/sas_*_prime.log."
            )
        log.info("Macro compile check passed for: %s", _REQUIRED_MACROS)

    # ------------------------------------------------------------------ public API
    def run_sas_pipeline(self, start_month: str, end_month: str) -> dict:
        """Run %run_months over [start_month, end_month], both 'YYYY-MM'."""
        s_cm = iso_to_campaign_month(start_month)
        e_cm = iso_to_campaign_month(end_month)
        log.info(
            "run_sas_pipeline: start=%s (sas=%s) end=%s (sas=%s)",
            start_month, s_cm.sas_reportdate, end_month, e_cm.sas_reportdate,
        )
        return self._submit(
            f"%run_months(start={s_cm.sas_reportdate}, end={e_cm.sas_reportdate});",
            label=f"run_months_{start_month}_to_{end_month}",
        )

    def run_one_month_sas(self, campaign_month: str) -> dict:
        """Run %run_one_month for a single 'YYYY-MM' (full pipeline:
        readmailfile → getresponse → finalresponse → rank → rollup → export)."""
        cm = iso_to_campaign_month(campaign_month)
        expected_csv = cm.rollup_filename
        log.info(
            "run_one_month_sas: month=%s sas_reportdate=%s sas_label=%s "
            "expected_table=%s expected_csv=%s export_folder=%s",
            campaign_month, cm.sas_reportdate, cm.sas_label,
            f"{cm.sas_label}_rollup", expected_csv, self.export_folder,
        )
        return self._submit(
            f"%run_one_month({cm.sas_reportdate});",
            label=f"run_one_month_{campaign_month}",
        )

    def run_one_month_from_existing_sas(self, campaign_month: str) -> dict:
        """Reaggregate from existing trm.<label>_finalresponse. Skips
        readmailfile / getresponse / finalresponse. ~30s instead of ~5min
        per month. Errors if trm.<label>_finalresponse does not exist.
        """
        cm = iso_to_campaign_month(campaign_month)
        log.info(
            "run_one_month_from_existing_sas: month=%s sas_reportdate=%s "
            "sas_label=%s expected_csvs=[%s, %s, %s]",
            campaign_month, cm.sas_reportdate, cm.sas_label,
            cm.rollup_filename, cm.decile_sc_filename, cm.decile_port_filename,
        )
        return self._submit(
            f"%run_one_month_from_existing({cm.sas_reportdate});",
            label=f"run_one_month_from_existing_{campaign_month}",
        )

    def pull_sas_table(self, table_name: str, libref: str = "WORK") -> pd.DataFrame:
        """Pull a SAS table into a pandas DataFrame."""
        if self.cnx is None:
            raise RuntimeError("SAS not connected. Call connect() first.")
        log.info("sd2df(table=%s, libref=%s)", table_name, libref)
        return self.cnx.SAS_Connection.sd2df(table=table_name, libref=libref)


# Module-level convenience wrappers --------------------------------------------------
@contextmanager
def open_sas(cfg: dict | None = None) -> Iterator[SASRunner]:
    cfg = cfg or load_config()
    runner = SASRunner(cfg)
    try:
        runner.connect()
        yield runner
    finally:
        runner.disconnect()


def run_sas_pipeline(start_month: str, end_month: str, cfg: dict | None = None) -> dict:
    with open_sas(cfg) as r:
        return r.run_sas_pipeline(start_month, end_month)


def run_one_month_sas(campaign_month: str, cfg: dict | None = None) -> dict:
    with open_sas(cfg) as r:
        return r.run_one_month_sas(campaign_month)


def pull_sas_table(table_name: str, libref: str = "WORK", cfg: dict | None = None) -> pd.DataFrame:
    with open_sas(cfg) as r:
        return r.pull_sas_table(table_name, libref)
