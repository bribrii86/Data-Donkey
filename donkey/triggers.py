"""Call this week: one ranked list per area from every trigger Data Donkey holds, for the Monday list.

Sources today: growth_* events (snapshot diffs) and ucc_filing events (new financing, and copier leases nearing their
estimated end). Apps add their own triggers (permits, new licenses) by passing scorer functions, so nothing here has
to change when a new source arrives."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

from . import areas, ucc
from .common import database_path, parse_date
from .common import norm as _norm

__all__ = ["SCORES", "Scorer", "call_this_week"]

# Points per trigger. Tune here; a business at min_score or more is a "call this week".
SCORES = {
    "ucc_new_copier_lessor": 4,  # just financed office equipment with a copier lessor
    "ucc_new_equipment_finance": 3,
    "ucc_new_bank": 1,
    "ucc_new_other": 1,
    "ucc_lease_ending": 5,  # copier lessor filing whose estimated lease end is within LEASE_LEAD_DAYS
    "sba_loan": 3,  # SBA 7(a)/504 loan approved within SBA_LOOKBACK_DAYS: financing to grow or buy a business
}
LEASE_LEAD_DAYS = 180  # start calling six months before the estimated lease end
SBA_LOOKBACK_DAYS = 180  # SBA publishes quarterly, so a loan is weeks to months old when it arrives
REASONS = {
    "home_to_office": "moved from a home address to an office",
    "new_business": "new business on the list",
    "moved": "moved to a new address",
    "new_website": "new website",
    "new_phone": "new business phone",
    "category_change": "changed line of work",
    "ucc_new_copier_lessor": "new financing with a copier lessor",
    "ucc_new_equipment_finance": "new equipment financing",
    "ucc_new_bank": "new bank lien",
    "ucc_new_other": "new UCC filing",
    "ucc_lease_ending": "copier lease likely ending",
    "sba_loan": "SBA loan approved",
}

# A scorer gets (read-only connection, area or None, as_of date, since date) and returns hits:
# {"name", "zip", "town", "score", "signal", "date"} plus anything else worth showing.
Scorer = Callable[[sqlite3.Connection, "dict | None", date, date], Iterable[dict]]


def _key(name: str, zip_code: str) -> str:
    return f"{_norm(name)}|{re.sub(r'[^0-9]', '', zip_code or '')[:5]}"


def _growth_hits(con: sqlite3.Connection, area: dict | None, as_of: date, since: date) -> Iterable[dict]:
    rows = con.execute(
        "SELECT * FROM events WHERE kind LIKE 'growth\\_%' ESCAPE '\\' AND event_date >= ? AND event_date <= ?",
        (since.isoformat(), as_of.isoformat()),
    )
    for row in rows:
        details = json.loads(row["details_json"])
        if area and not areas.contains(area, details.get("zip", ""), row["town"]):
            continue
        yield {"name": details["name"], "zip": details.get("zip", ""), "town": row["town"],
               "score": int(details["score"]), "signal": row["kind"].removeprefix("growth_"), "date": row["event_date"],
               "street": details.get("after_street", ""), "home_based": details.get("home_based", False)}


def _town_in_name(area: dict, name: str) -> str:
    """An area town named in a business name ("Casa Grande Dairy Co"), longest first; not "<town> County"."""
    folded = (name or "").casefold()
    for city in sorted(area["cities"], key=len, reverse=True):
        if re.search(rf"\b{re.escape(city)}\b(?!\s+county)", folded):
            return city.title()
    return ""


def _ucc_hits(con: sqlite3.Connection, area: dict | None, as_of: date, since: date) -> Iterable[dict]:
    rows = con.execute(
        """SELECT events.*, businesses.name AS business_name, businesses.zip AS business_zip,
        businesses.city AS business_city FROM events LEFT JOIN businesses ON businesses.id = events.business_id
        WHERE events.kind = 'ucc_filing'"""
    )
    for row in rows:
        details = json.loads(row["details_json"])
        if "lessor_type" not in details:  # stored before v0.5.2 in a database opened read-only here
            details.update(ucc.enrich({"secured_party": details.get("secured_party", ""),
                                       "event_date": row["event_date"] or ""}))
        name = row["business_name"] or details.get("name", "")
        zip_code = details.get("zip") or row["business_zip"] or ""
        town = row["town"] or row["business_city"] or ""
        location = "known" if zip_code or town else "unknown"
        if area and location == "unknown":
            # The SOS CSV has no address. Keep the filing in area runs: use a territory town named in the debtor
            # name if there is one, else show it as location unknown for the rep to check.
            town = _town_in_name(area, name)
            location = "debtor name" if town else "unknown"
        elif area and not areas.contains(area, zip_code, town):
            continue
        filed = parse_date(row["event_date"]) if row["event_date"] else None
        kind = details.get("lessor_type", "")
        base = {"name": name, "zip": zip_code, "town": town, "secured_party": details.get("secured_party", ""),
                "filing_number": details.get("filing_number", ""), "location": location}
        if filed and since <= filed <= as_of:
            signal = f"ucc_new_{kind}" if f"ucc_new_{kind}" in SCORES else ""
            if signal and SCORES[signal]:
                yield {**base, "score": SCORES[signal], "signal": signal, "date": filed.isoformat()}
        end = parse_date(details.get("lease_end_est", "")) if details.get("lease_end_est") else None
        if kind == "copier_lessor" and end and end - timedelta(days=LEASE_LEAD_DAYS) <= as_of <= end + timedelta(days=30):
            yield {**base, "score": SCORES["ucc_lease_ending"], "signal": "ucc_lease_ending", "date": end.isoformat(),
                   "lease_end_est": end.isoformat()}


def _sba_hits(con: sqlite3.Connection, area: dict | None, as_of: date, since: date) -> Iterable[dict]:
    start = (as_of - timedelta(days=SBA_LOOKBACK_DAYS)).isoformat()
    rows = con.execute(
        """SELECT events.*, businesses.name AS business_name, businesses.zip AS business_zip FROM events
        LEFT JOIN businesses ON businesses.id = events.business_id
        WHERE events.kind = 'sba_loan' AND events.event_date >= ? AND events.event_date <= ?""",
        (start, as_of.isoformat()),
    )
    for row in rows:
        details = json.loads(row["details_json"])
        zip_code = details.get("zip") or row["business_zip"] or ""
        if area and not areas.contains(area, zip_code, row["town"] or ""):
            continue
        yield {"name": row["business_name"] or details.get("name", ""), "zip": zip_code, "town": row["town"] or "",
               "score": SCORES["sba_loan"], "signal": "sba_loan", "date": row["event_date"],
               "amount": details.get("amount", ""), "lender": details.get("lender", "")}


def call_this_week(
    area: str | None = None,
    as_of: str | date | None = None,
    days: int = 14,
    min_score: int = 3,
    scorers: Iterable[Scorer] = (),
    db_path: str | Path | None = None,
) -> list[dict]:
    """Businesses to call this week in a named area (areas.json), highest score first. Points from all triggers in
    the last `days` days (lease-ending triggers count while the lease end is near; SBA loans for
    SBA_LOOKBACK_DAYS) are added per business."""
    today = as_of if isinstance(as_of, date) else (parse_date(as_of) if as_of else date.today())
    if today is None:
        raise ValueError(f"bad as_of date {as_of!r}")
    since = today - timedelta(days=days)
    area_def = areas.load(area) if area else None
    con = sqlite3.connect(f"{database_path(db_path).as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        hits = [hit for scorer in (_growth_hits, _ucc_hits, _sba_hits, *scorers) for hit in scorer(con, area_def, today, since)]
    finally:
        con.close()
    totals: dict[str, dict] = {}
    for hit in hits:
        item = totals.setdefault(_key(hit["name"], hit["zip"]), {
            "name": hit["name"], "zip": hit["zip"], "town": hit["town"], "score": 0, "signals": [], "reasons": [],
            "last_date": "", "details": [],
        })
        item["score"] += int(hit["score"])
        item["signals"].append(hit["signal"])
        item["reasons"].append(REASONS.get(hit["signal"], hit.get("reason", hit["signal"])))
        item["last_date"] = max(item["last_date"], hit["date"])
        item["details"].append({k: v for k, v in hit.items() if k not in {"name", "zip", "town"}})
    ranked = [item for item in totals.values() if item["score"] >= min_score]
    return sorted(ranked, key=lambda item: (-item["score"], item["name"]))
