"""Thin wrapper around the company's C1BConnections / saspy submission flow.

We deliberately keep this layer minimal. SAS remains the trusted data engine;
this module only orchestrates connect → submit(%include) → submit(%run_*) →
optionally sd2df → disconnect.

Three public entrypoints:
    run_sas_pipeline(start_month, end_month) -> SAS log
    run_one_month_sas(campaign_month)        -> SAS log
    pull_sas_table(table_name, libref='WORK')-> pandas.DataFrame

Both pipeline entrypoints expect strings in 'YYYY-MM' form. They translate to
SAS date9. (e.g. '01JAN2025') before invoking the macros, so the caller never
has to think about SAS date formats.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pandas as pd

from .utils import CampaignMonth, iso_to_campaign_month, load_config

log = logging.getLogger(__name__)


def _import_agora():
    """Lazy import so dev machines without Agora can still load the package.

    The exact sys.path append is preserved from the notebook so we do not
    perturb the company SAS connector contract.
    """
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

    # ------------------------------------------------------------------ lifecycle
    def connect(self) -> None:
        C1BConnections = _import_agora()
        self.cnx = C1BConnections()
        self.cnx.connect_to_SAS()
        log.info("SAS connection established")
        self._prime_environment()

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
    def _submit(self, code: str) -> dict:
        if self.cnx is None:
            raise RuntimeError("SAS not connected. Call connect() first.")
        result = self.cnx.SAS_Connection.submit(code)
        # The Agora submit() returns a dict {'LOG': ..., 'LST': ...}. Tail the
        # log into Python's logger so failures are visible without opening SAS.
        log.debug("SAS LOG tail:\n%s", (result.get("LOG", "") or "")[-2000:])
        return result

    def _prime_environment(self) -> None:
        """Assign trm libname, set &folder, and %include the macro file."""
        pipeline = str(self.pipeline_file).replace("\\", "/")
        prelude = f"""
            libname {self.trm_libname} '{self.trm_libpath}';
            %let folder = {self.export_folder};
            %include "{pipeline}";
        """
        self._submit(prelude)
        log.info("SAS environment primed (libname + %%include)")

    # ------------------------------------------------------------------ public API
    def run_sas_pipeline(self, start_month: str, end_month: str) -> dict:
        """Run %run_months over [start_month, end_month], both 'YYYY-MM'."""
        s = iso_to_campaign_month(start_month).sas_reportdate
        e = iso_to_campaign_month(end_month).sas_reportdate
        log.info("Submitting %%run_months(start=%s, end=%s)", s, e)
        return self._submit(f"%run_months(start={s}, end={e});")

    def run_one_month_sas(self, campaign_month: str) -> dict:
        """Run %run_one_month for a single 'YYYY-MM'."""
        cm = iso_to_campaign_month(campaign_month)
        log.info("Submitting %%run_one_month(%s)", cm.sas_reportdate)
        return self._submit(f"%run_one_month({cm.sas_reportdate});")

    def pull_sas_table(self, table_name: str, libref: str = "WORK") -> pd.DataFrame:
        """Pull a SAS table into a pandas DataFrame.

        Intended for small/aggregated tables (rollups). DO NOT use on
        customer-level mailfile or finalresponse — those are 20M rows and
        contain PII.
        """
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
