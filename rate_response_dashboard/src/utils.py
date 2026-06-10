"""Shared helpers: config, logging, month parsing, paths."""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

_MONTH_ABBR = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_MONTH_NUM_TO_ABBR = {v: k for k, v in _MONTH_ABBR.items()}

# Filenames produced by SAS: exp_JAN25_rollup.csv, exp_JAN25_decile.csv ...
_ROLLUP_FILE_RE = re.compile(
    r"^exp_(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<yy>\d{2})_rollup\.csv$",
    re.IGNORECASE,
)
_DECILE_FILE_RE = re.compile(
    r"^exp_(?P<mon>JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(?P<yy>\d{2})_decile\.csv$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CampaignMonth:
    """One campaign month, with two canonical representations."""
    year: int
    month: int

    @property
    def iso(self) -> str:
        """e.g. '2025-01' — used as partition key."""
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def sas_label(self) -> str:
        """e.g. 'JAN25' — matches SAS monyy5. used in filenames/table names."""
        return f"{_MONTH_NUM_TO_ABBR[self.month]}{self.year % 100:02d}"

    @property
    def sas_reportdate(self) -> str:
        """e.g. '01JAN2025' — matches SAS date9. for %run_one_month."""
        return f"01{_MONTH_NUM_TO_ABBR[self.month]}{self.year:04d}"

    @property
    def rollup_table_name(self) -> str:
        """e.g. 'JAN25_rollup' — name of the SAS WORK table the rollup macro builds."""
        return f"{self.sas_label}_rollup"

    @property
    def rollup_filename(self) -> str:
        """e.g. 'exp_JAN25_rollup.csv' — filename SAS proc export writes."""
        return f"exp_{self.sas_label}_rollup.csv"

    @property
    def decile_filename(self) -> str:
        """e.g. 'exp_JAN25_decile.csv' — decile-grain SAS export filename."""
        return f"exp_{self.sas_label}_decile.csv"


def _parse_with(pattern: re.Pattern, name: str) -> CampaignMonth | None:
    m = pattern.match(os.path.basename(name))
    if not m:
        return None
    mon = _MONTH_ABBR[m.group("mon").upper()]
    yy = int(m.group("yy"))
    year = 2000 + yy if yy < 80 else 1900 + yy
    return CampaignMonth(year=year, month=mon)


def parse_rollup_filename(name: str) -> CampaignMonth | None:
    """Parse 'exp_JAN25_rollup.csv' → CampaignMonth(2025, 1). None if no match.

    Two-digit year window: 00-79 → 20YY, 80-99 → 19YY.
    """
    return _parse_with(_ROLLUP_FILE_RE, name)


def parse_decile_filename(name: str) -> CampaignMonth | None:
    """Parse 'exp_JAN25_decile.csv' → CampaignMonth(2025, 1)."""
    return _parse_with(_DECILE_FILE_RE, name)


def iso_to_campaign_month(iso: str) -> CampaignMonth:
    y, m = iso.split("-")
    return CampaignMonth(year=int(y), month=int(m))


def load_config(path: str | Path = "config/config.yaml") -> dict:
    """Read yaml config from disk."""
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def setup_logging(cfg: dict) -> logging.Logger:
    log_cfg = cfg.get("logging", {})
    log_file = log_cfg.get("file", "./data/logs/app.log")
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    level = getattr(logging, log_cfg.get("level", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
        force=True,
    )
    return logging.getLogger("rate_response")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def ensure_dir(p: str | Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p
