"""ReportPackage → HTML string.

Jinja2 with a single self-contained template + inline CSS. No external
asset links, so the report is portable (email, attach, save to share).

Custom filters here normalize the rate / count / bps display rules so the
template stays declarative.
"""
from __future__ import annotations

import math
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..facts import ReportPackage


_TEMPLATE_DIR = Path(__file__).parent / "templates"
_TEMPLATE_NAME = "report.html.j2"


def render_html(pkg: ReportPackage) -> str:
    env = _env()
    tmpl = env.get_template(_TEMPLATE_NAME)
    return tmpl.render(pkg=pkg)


def _env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["pct"] = _f_pct
    env.filters["bps"] = _f_bps
    env.filters["pct_change"] = _f_pct_change
    env.filters["intk"] = _f_intk
    env.filters["ratio"] = _f_ratio
    env.filters["dash_if_none"] = _f_dash
    env.filters["direction_class"] = _f_direction_class
    env.filters["maturity_class"] = _f_maturity_class
    return env


# Use the Unicode minus sign so negative numbers don't get confused with
# stray hyphens in narrative text — small detail but improves readability.
_MINUS = "−"


def _f_pct(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v * 100:.{digits}f}%".replace("-", _MINUS)


def _f_bps(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    sign = _MINUS if v < 0 else "+"
    return f"{sign}{abs(v):,.0f} bps"


def _f_pct_change(v, digits: int = 1) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    sign = _MINUS if v < 0 else "+"
    return f"{sign}{abs(v) * 100:.{digits}f}%"


def _f_intk(v) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    n = float(v)
    if abs(n) >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n/1_000:.1f}K"
    return f"{n:,.0f}"


def _f_ratio(v, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "—"
    return f"{v:.{digits}f}×"


def _f_dash(v) -> str:
    return "—" if v is None else str(v)


def _f_direction_class(direction: str) -> str:
    return {
        "up": "dir-up",
        "down": "dir-down",
        "flat": "dir-flat",
    }.get(direction, "dir-flat")


def _f_maturity_class(status: str) -> str:
    return {
        "full": "mat-full",
        "partial": "mat-partial",
        "unknown": "mat-unknown",
    }.get(status, "mat-unknown")
