"""UCC filing tags: what kind of secured party filed, and for copier and equipment lessors an estimated lease window.
Arizona records no collateral, so the secured party's name is the only clue to what was financed."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

from .common import parse_date

__all__ = ["LEASE_MONTHS", "backfill", "classify", "enrich"]

# Usual office-equipment lease terms: calls start 36 months after filing, most leases end at 60.
LEASE_MONTHS = {"window_start": 36, "end": 60}

# Checked in order; the first match wins. Names as they appear on filings (upper or mixed case).
_TYPES: list[tuple[str, re.Pattern[str]]] = [
    ("copier_lessor", re.compile(
        r"great\s*america|de\s*lage\s*landen|\bdll\b|canon\s+financial|xerox\s+financial|ricoh|konica|"
        r"sharp\s+business|toshiba\s+(financial|business|america)|kyocera|leaf\s+(commercial|capital)|"
        r"marlin|wells\s+fargo\s+(vendor|equipment)|u\.?\s?s\.?\s+(bank|bancorp)\s+equipment|flexprint|"
        r"pitney|quadient|neopost|tiaa\s+commercial|ge\s+capital\s+information", re.I)),
    ("equipment_finance", re.compile(
        r"equipment\s+financ|leasing|lease|financial\s+services|commercial\s+financ|ascentium|balboa|"
        r"navitas|dext|crestmark|first\s+american\s+equipment|asset\s+financ|financing\s+services|flex\s+financial|"
        r"med\s*one|motorola\s+solutions\s+credit", re.I)),
    ("ag", re.compile(r"deere|farm\s+credit|agwest|cnh|kubota|ag\s+credit|agri", re.I)),
    ("auto", re.compile(r"ally\s+(bank|financial)|motor\s+credit|toyota|ford\s+credit|gm\s+financial|nissan", re.I)),
    ("bank", re.compile(
        r"bank|\bbanc\b|bancorp|credit\s+union|national\s+association|\bn\.?a\.?$|chase|truist|capital\s+one|"
        r"morgan\s+stanley", re.I)),
    ("mca", re.compile(
        r"funding|advance|capital\s+(llc|group|partners)|corporation\s+service\s+company|c\s?t\s+corporation|"
        r"merchant|kapitus|ondeck|credibly|fundbox|bluevine|libertas|yellowstone", re.I)),
    ("government", re.compile(r"internal\s+revenue|department\s+of|state\s+of|county|united\s+states|\bsba\b|small\s+business\s+admin", re.I)),
]


def classify(secured_party: str) -> str:
    """copier_lessor, equipment_finance, ag, auto, mca, government, bank, other, or '' when no secured party."""
    party = (secured_party or "").strip()
    if not party:
        return ""
    for kind, pattern in _TYPES:
        if pattern.search(party):
            return kind
    return "other"


def _add_months(day: date, months: int) -> date:
    month = day.month - 1 + months
    year, month = day.year + month // 12, month % 12 + 1
    return date(year, month, min(day.day, 28))


def enrich(values: dict[str, str]) -> dict[str, str]:
    """Tags for one filing row: lessor_type, plus lease_window_start and lease_end_est for lessors."""
    kind = classify(values.get("secured_party", ""))
    out = {"lessor_type": kind, "lease_window_start": "", "lease_end_est": ""}
    filed = parse_date(values.get("event_date", "")) if values.get("event_date") else None
    if filed and kind in {"copier_lessor", "equipment_finance"}:
        out["lease_window_start"] = _add_months(filed, LEASE_MONTHS["window_start"]).isoformat()
        out["lease_end_est"] = _add_months(filed, LEASE_MONTHS["end"]).isoformat()
    return out


def backfill(con: sqlite3.Connection) -> int:
    """Tag ucc_filing events stored before v0.5.2 (no lessor_type yet). Re-ingesting the same file is skipped by its
    hash, so without this an older donkey.db would never get lessor tags or lease dates. Returns events updated."""
    rows = con.execute(
        """SELECT id, event_date, details_json FROM events
        WHERE kind = 'ucc_filing' AND instr(details_json, '"lessor_type"') = 0"""
    ).fetchall()
    for event_id, event_date, details_json in rows:
        details = json.loads(details_json or "{}")
        details.update(enrich({"secured_party": details.get("secured_party", ""), "event_date": event_date or ""}))
        con.execute("UPDATE events SET details_json = ? WHERE id = ?", (json.dumps(details), event_id))
    if rows:
        con.commit()
    return len(rows)
