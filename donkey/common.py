"""Shared paths and text helpers. Copied from the Stephanie app (prospects._norm, _parse_date) at the repo split
so Data Donkey no longer imports the app; matching results are unchanged for the ACP setup."""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from pathlib import Path

# One store per computer, shared by every app that reads it. DONKEY_DB overrides.
DEFAULT_DB = Path.home() / "DataDonkey" / "donkey.db"

_STOP = {"inc", "llc", "llp", "pllc", "pc", "corp", "corporation", "company", "co", "ltd", "the", "of", "and", "&",
         "dds", "md", "pa", "plc"}
_ABBR = {"ctr": "center", "cntr": "center", "med": "medical", "hosp": "hospital", "svc": "services", "svcs": "services",
         "assn": "association", "mgmt": "management", "dist": "district", "sch": "school", "intl": "international"}


def database_path(path: str | Path | None = None) -> Path:
    """The given path, else DONKEY_DB, else DEFAULT_DB."""
    return Path(path or os.environ.get("DONKEY_DB") or DEFAULT_DB).expanduser().resolve()


def norm(name: str) -> str:
    """Business name for matching: "Copper Mesa Dental, PLLC | Casa Grande" -> "copper mesa dental"."""
    name = re.sub(r"arizona\s+city", "arizonacity", name.split("|")[0], flags=re.I)  # a town, not "Arizona" + "City"
    words = re.sub(r"[^a-z0-9 ]", " ", re.sub(r"['.]", "", name.lower())).split()
    return " ".join(_ABBR.get(w, w) for w in words if w not in _STOP)


def parse_date(s: str) -> date | None:
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y%m%d", "%d-%b-%Y", "%b %d, %Y", "%b, %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    return None
