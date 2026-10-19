"""Call this week: one ranked list per area from every trigger Data Donkey holds, for the Monday list.

Sources today: growth_* events (snapshot diffs), ucc_filing events (new financing, and copier leases nearing their
estimated end), sba_loan events, new_llc events (Pinal County formation notices) and board_minutes events (public
meeting documents about office equipment, print, IT or a related RFP or budget). Every business also gets a
who-decides answer (donkey.locality) under "locality". Apps add their own triggers (permits, new licenses) by passing scorer functions, so nothing here has
to change when a new source arrives."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable

from . import areas, locality, ucc
from .common import database_path, parse_date
from .common import norm as _norm

__all__ = ["SCORES", "Scorer", "call_this_week", "lease_calendar", "looks_print_heavy"]

# Points per trigger. Tune here; a business at min_score or more is a "call this week".
SCORES = {
    "ucc_new_copier_lessor": 4,  # just financed office equipment with a copier lessor
    "ucc_new_equipment_finance": 3,
    "ucc_new_bank": 1,
    "ucc_new_other": 1,
    "ucc_lease_ending": 5,  # copier lessor filing whose estimated lease end is within LEASE_LEAD_DAYS
    "sba_loan": 3,  # SBA 7(a)/504 loan approved within SBA_LOOKBACK_DAYS: financing to grow or buy a business
    "new_llc": 3,  # new LLC or corporation formed within NEW_LLC_LOOKBACK_DAYS: setting up an office now
    "new_llc_agent": 3,  # same, but the notice only gives the statutory agent's address (often a lawyer's office)
    "new_license": 3,  # city business license issued within NEW_LICENSE_LOOKBACK_DAYS (not a Jan 1 renewal)
    "board_intent_plus": 4,  # a public body plans to buy, opened an RFP, or has a contract or lease ending
    "board_vendor": 3,  # a public body named its current vendor or contract terms
    "board_intent_minus": 0,  # declined or tabled a purchase: shown as a reason, never a reason to call
    "board_info": 0,
    # v0.6.2 boosts: added only to a business already on the list, never a reason to call on their own
    "print_heavy": 1,  # law, accounting, medical, real estate/title, insurance, schools: print a lot
    "hiring": 2,  # the who-decides check found it hiring or opening a location (new desks)
}
BOOSTS = ("print_heavy", "hiring")
LEASE_LEAD_DAYS = 180  # start calling six months before the estimated lease end
GROWTH_LOOKBACK_DAYS = 180  # a Crustdata export's 6-month headcount growth stays useful about this long
SBA_LOOKBACK_DAYS = 180  # SBA publishes quarterly, so a loan is weeks to months old when it arrives
BOARD_LOOKBACK_DAYS = 45  # boards move slowly; a vote or RFP stays worth acting on for weeks
NEW_LICENSE_LOOKBACK_DAYS = 60  # a records request arrives weeks after issue; a new license stays worth a call ~2 months
NEW_LLC_LOOKBACK_DAYS = 60  # the notice runs up to 60 days after approval, so the formation date is already weeks old
# LLCs named after a street address ("408 N. Sacaton Street LLC") or called holdings are usually a building's owner,
# not a business that will set up an office; they stay in the store but don't score.
# The street word must end the name (bar the entity suffix), so "2 Brothers Way Trucking LLC" still scores.
_HOLDING = re.compile(
    r"^\s*\d+\s+(\S+\s+){0,4}(st|street|rd|road|ave|avenue|hwy|highway|ln|lane|dr|drive|blvd|boulevard|way|pkwy|"
    r"parkway|ct|court|pl|place|cir|circle|trl|trail)\.?,?(\s+(llc|l\.l\.c\.|inc|corp|co|lp|llp)\.?)?\s*$"
    r"|\bholdings?\b", re.IGNORECASE)
# Self-serve machines inside other stores (ATMs, coin and crypto kiosks, DVD boxes, parcel lockers) appear in
# Overture as their own places but have no office to sell to; their growth signals don't score. Brands match only at
# the start of the name, and a name with a company or service word ("ATM Repair LLC", "Redbox Marketing Group") is a
# business, not a machine.
_KIOSK = re.compile(
    r"^\s*(coinme|coinstar|coinflip|coinhub|libertyx|bitstop|rockitcoin|byte federal|athena bitcoin|bitcoin depot|"
    r"ecoatm|keyme|redbox|amazon (hub )?locker)\b|\b(bitcoin|crypto) atm\b|\batm\b", re.IGNORECASE)
_NOT_KIOSK = re.compile(
    r"\b(llc|l\.l\.c|inc|corp|co|company|pllc|group|services?|repair|consulting|marketing|sales|solutions|dealer|"
    r"office|jewelry|supply)\b\.?", re.IGNORECASE)
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
    "new_llc": "new business formed (formation notice)",
    "new_llc_agent": "new business formed (formation notice; address is the statutory agent's, look it up first)",
    "new_license": "new city business license",
    "board_intent_plus": "board meeting: plans to buy or contract ending",
    "board_vendor": "board meeting named their vendor or contract",
    "board_intent_minus": "board meeting: purchase declined or tabled",
    "board_info": "board meeting mentioned equipment or IT",
    "print_heavy": "heavy-print line of work",
    "hiring": "hiring or expanding",
}
_PRINT_HEAVY = re.compile(
    r"\b(law(?! enforcement)|attorneys?|lawyers?|legal (services?|aid)|paralegal|cpas?|"
    r"accounting|accountants?|tax (services?|prep|preparation)|title (company|agency|insurance)|escrow|realty|"
    r"real estate|realtors?|mortgage|insurance|preschool|learning center|college)\b"
    r"|\b(elementary|middle|high|charter|private|public|montessori|christian|prep|preparatory) (schools?|academy)\b"
    r"|\b(schools?|school district|unified|university)\b", re.IGNORECASE)
_NOT_PRINT_HEAVY = re.compile(r"\b(driving|truck|cdl|dance|karate|martial|swim\w*|beauty|barber|cosmetology|pizza|"
                              r"grill|sports?|chicken)\b", re.IGNORECASE)
_HIRING = re.compile(r"\b(hiring|job (posts?|openings?|listings?)|recruit\w*|new (location|office|branch)|expan\w*|"
                     r"second location|opening (a|another)\b)", re.IGNORECASE)
_NOT_HIRING = re.compile(r"\b(no|not|none|nothing)\s+(currently\s+|actively\s+|planned\s+|planning\s+)?"
                         r"(hiring|expan\w*|job (posts?|openings?|listings?)|new (location|office|branch))\w*( plans?)?",
                         re.IGNORECASE)
_DEAD_FILING = re.compile(r"terminat|lapse|released|expired", re.IGNORECASE)

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
        if looks_like_kiosk(details["name"]) or (area and not areas.contains(area, details.get("zip", ""), row["town"])):
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


def _ucc_rows(con: sqlite3.Connection, area: dict | None) -> Iterable[tuple[dict, dict, date | None]]:
    """(base fields, filing details, filed date) per UCC filing in the area, with the location rules below."""
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
        base = {"name": name, "zip": zip_code, "town": town, "secured_party": details.get("secured_party", ""),
                "filing_number": details.get("filing_number", ""), "location": location}
        yield base, details, filed


def _ucc_hits(con: sqlite3.Connection, area: dict | None, as_of: date, since: date) -> Iterable[dict]:
    for base, details, filed in _ucc_rows(con, area):
        kind = details.get("lessor_type", "")
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


def looks_like_holding(name: str) -> bool:
    return bool(_HOLDING.search(name or ""))


def looks_like_kiosk(name: str) -> bool:
    return bool(_KIOSK.search(name or "")) and not _NOT_KIOSK.search(name or "")


def _new_llc_hits(con: sqlite3.Connection, area: dict | None, as_of: date, since: date) -> Iterable[dict]:
    start = (as_of - timedelta(days=NEW_LLC_LOOKBACK_DAYS)).isoformat()
    rows = con.execute(
        "SELECT * FROM events WHERE kind = 'new_llc' AND event_date >= ? AND event_date <= ?",
        (start, as_of.isoformat()),
    )
    for row in rows:
        details = json.loads(row["details_json"])
        name, zip_code = details.get("name", ""), details.get("zip", "")
        if looks_like_holding(name) or (area and not areas.contains(area, zip_code, row["town"] or "")):
            continue
        signal = "new_llc_agent" if details.get("address_type", "").strip().lower() == "agent" else "new_llc"
        yield {"name": name, "zip": zip_code, "town": row["town"] or "", "score": SCORES[signal],
               "signal": signal, "date": row["event_date"], "agent": details.get("agent", ""),
               "address_type": details.get("address_type", ""), "newspaper": details.get("newspaper", "")}


def _license_hits(con: sqlite3.Connection, area: dict | None, as_of: date, since: date) -> Iterable[dict]:
    """v0.6.5: city business licenses (source map city-licenses). Cities reissue every renewal on Jan 1 and the list
    has no new/renewal flag, so a Jan 1 issue date never scores."""
    start = (as_of - timedelta(days=NEW_LICENSE_LOOKBACK_DAYS)).isoformat()
    rows = con.execute("SELECT * FROM events WHERE kind = 'new_license' AND event_date >= ? AND event_date <= ?",
                       (start, as_of.isoformat()))
    for row in rows:
        details = json.loads(row["details_json"])
        name, zip_code = details.get("name", ""), details.get("zip", "")
        if row["event_date"][5:10] == "01-01" or looks_like_kiosk(name):
            continue
        if area and not areas.contains(area, zip_code, row["town"] or ""):
            continue
        yield {"name": name, "zip": zip_code, "town": row["town"] or "", "score": SCORES["new_license"],
               "signal": "new_license", "date": row["event_date"], "license_number": details.get("license_number", ""),
               "classification": details.get("classification", "")}


def _board_hits(con: sqlite3.Connection, area: dict | None, as_of: date, since: date) -> Iterable[dict]:
    start = (as_of - timedelta(days=BOARD_LOOKBACK_DAYS)).isoformat()
    rows = con.execute("SELECT * FROM events WHERE kind = 'board_minutes' AND event_date >= ? AND event_date <= ?",
                       (start, as_of.isoformat()))
    for row in rows:
        details = json.loads(row["details_json"])
        if area and not areas.contains(area, details.get("zip", ""), row["town"] or ""):
            continue  # no town or ZIP inside the area: not shown (the body list should be the territory's)
        signal = f"board_{details.get('flag') or 'info'}"
        if signal not in SCORES:
            signal = "board_info"
        yield {"name": details.get("name", ""), "zip": details.get("zip", ""), "town": row["town"] or "", "score": SCORES[signal],
               "signal": signal, "date": row["event_date"], "summary": details.get("summary", ""),
               "doc_url": details.get("doc_url", ""), "vendor": details.get("vendor", "")}


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
    SBA_LOOKBACK_DAYS, formation notices for NEW_LLC_LOOKBACK_DAYS, city licenses for NEW_LICENSE_LOOKBACK_DAYS) are
    added per business."""
    today = as_of if isinstance(as_of, date) else (parse_date(as_of) if as_of else date.today())
    if today is None:
        raise ValueError(f"bad as_of date {as_of!r}")
    since = today - timedelta(days=days)
    area_def = areas.load(area) if area else None
    con = sqlite3.connect(f"{database_path(db_path).as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        board = list(_board_hits(con, area_def, today, since))
        hits = [hit for scorer in (_growth_hits, _ucc_hits, _sba_hits, _new_llc_hits, _license_hits, *scorers)
                for hit in scorer(con, area_def, today, since)] + board
        ranked = _rank(hits, min_score)
        for item, answer in zip(ranked, locality.for_items(ranked, con, today)):
            item["locality"] = answer
            notes = [h for h in board if _norm(h["name"]) == _norm(item["name"])]
            item["board_note"] = "; ".join(f"{h['date']}: {h['summary']}" for h in sorted(
                notes, key=lambda h: h["date"], reverse=True)[:2] if h["summary"])
        categories, growing = _categories(con), _growing(con, today)
        for item in ranked:
            _boost(item, categories.get(_key(item["name"], item["zip"]), ""),
                   _town_key(item["name"], item["town"]) in growing)
    finally:
        con.close()
    return sorted(ranked, key=lambda item: (-item["score"], item["name"]))


def looks_print_heavy(name: str, category: str = "") -> bool:
    text = f"{name} {category}".replace("_", " ")  # Overture categories: elementary_school, insurance_agency
    return (bool(_PRINT_HEAVY.search(text)) and not _NOT_PRINT_HEAVY.search(text)) or \
        locality.looks_medical(name, category)


def _boost(item: dict, category: str, growing: bool = False) -> None:
    """v0.6.2 boosts on a listed business: heavy-print line of work, and hiring or expanding per its who-decides
    answer (the initiative line) or, since v0.6.3, a Crustdata headcount that grew."""
    initiative = (item.get("locality") or {}).get("initiative") or ""
    hits = {"print_heavy": looks_print_heavy(item["name"], category),
            "hiring": growing or bool(_HIRING.search(_NOT_HIRING.sub("", initiative)))}  # drop "not hiring" first
    for signal, found in hits.items():
        if found and signal not in item["signals"]:
            item["score"] += SCORES[signal]
            item["signals"].append(signal)
            item["reasons"].append(REASONS[signal])


def _town_key(name: str, town: str) -> str:
    return f"{_norm(name)}|{(town or '').strip().casefold()}"


def _growing(con: sqlite3.Connection, today: date) -> set[str]:
    """Name + town of businesses whose headcount grew in the latest Crustdata export imported in the last 180 days
    (a newer export with flat growth cancels an older one; the export has no ZIP or street, so it matches on name and
    town)."""
    since = (today - timedelta(days=GROWTH_LOOKBACK_DAYS)).isoformat()
    rows = con.execute("""SELECT town, details_json FROM events WHERE kind = 'headcount_growth'
        AND event_date >= ? AND event_date <= ? ORDER BY event_date, id""", (since, today.isoformat()))
    latest: dict[str, bool] = {}
    for row in rows:
        details = json.loads(row["details_json"])
        try:
            grew = float(str(details.get("growth_6m") or 0).replace("%", "").replace(",", "")) > 0
        except ValueError:
            grew = False
        latest[_town_key(details.get("name", ""), row["town"] or "")] = grew
    return {key for key, grew in latest.items() if grew}


def _categories(con: sqlite3.Connection) -> dict[str, str]:
    """Category per name + ZIP from the latest snapshot of each list (Overture, town lists); first one seen wins."""
    try:
        rows = con.execute(
            """SELECT snapshot_rows.name, snapshot_rows.zip, snapshot_rows.category FROM snapshot_rows JOIN
            (SELECT label, MAX(id) AS id FROM snapshots GROUP BY label) latest ON latest.id = snapshot_rows.snapshot_id
            WHERE snapshot_rows.category <> ''""").fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[str, str] = {}
    for name, zip_code, category in rows:
        out.setdefault(_key(name, zip_code), category)
    return out


def lease_calendar(
    area: str | None = None,
    as_of: str | date | None = None,
    months: int = 12,
    all_lessors: bool = False,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Copier leases (with all_lessors, equipment-finance leases too) whose estimated end falls from 30 days ago to
    `months` months ahead, soonest first, each with its month and who decides. Estimates come from ucc.enrich (filing
    + 60 months), so treat the month as the time to call, not a contract date."""
    today = as_of if isinstance(as_of, date) else (parse_date(as_of) if as_of else date.today())
    if today is None:
        raise ValueError(f"bad as_of date {as_of!r}")
    if months < 1:
        raise ValueError("months must be 1 or more")
    start, stop = today - timedelta(days=30), ucc._add_months(today, months)
    kinds = {"copier_lessor", "equipment_finance"} if all_lessors else {"copier_lessor"}
    area_def = areas.load(area) if area else None
    con = sqlite3.connect(f"{database_path(db_path).as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows, seen = [], set()
        for base, details, filed in _ucc_rows(con, area_def):
            end = parse_date(details.get("lease_end_est", "")) if details.get("lease_end_est") else None
            if details.get("lessor_type") not in kinds or not end or not start <= end <= stop:
                continue
            if _DEAD_FILING.search(details.get("lien_status") or ""):
                continue
            # Amendments and continuations repeat a filing; show a business once per lessor and end month.
            key = (_key(base["name"], base["zip"]), _norm(base["secured_party"]), end.strftime("%Y-%m"))
            if key not in seen:
                seen.add(key)
                rows.append({**base, "month": end.strftime("%Y-%m"), "lease_end_est": end.isoformat(),
                             "lease_window_start": details.get("lease_window_start", ""),
                             "filed": filed.isoformat() if filed else "", "lessor_type": details["lessor_type"]})
        for row, answer in zip(rows, locality.for_items(rows, con, today)):
            row["who_decides"] = answer["value"]
    finally:
        con.close()
    return sorted(rows, key=lambda r: (r["lease_end_est"], r["name"]))


def _rank(hits: list[dict], min_score: int) -> list[dict]:
    totals: dict[str, dict] = {}
    for hit in sorted(hits, key=lambda h: not h["zip"]):  # rows with a ZIP first, so a board hit can join one
        key = _key(hit["name"], hit["zip"])
        if not hit["zip"]:  # a board document with no ZIP: join the same public body already listed
            key = next((k for k in totals if k.split("|")[0] == _norm(hit["name"])), key)
        item = totals.setdefault(key, {
            "name": hit["name"], "zip": hit["zip"], "town": hit["town"], "score": 0, "signals": [], "reasons": [],
            "last_date": "", "details": [],
        })
        if hit["signal"] in item["signals"]:  # one point set per kind: three Coinme kiosks in a ZIP aren't 3 new businesses
            continue
        item["score"] += int(hit["score"])
        item["signals"].append(hit["signal"])
        item["reasons"].append(REASONS.get(hit["signal"], hit.get("reason", hit["signal"])))
        item["last_date"] = max(item["last_date"], hit["date"])
        item["details"].append({k: v for k, v in hit.items() if k not in {"name", "zip", "town"}})
    ranked = [item for item in totals.values() if item["score"] >= min_score]
    return sorted(ranked, key=lambda item: (-item["score"], item["name"]))
