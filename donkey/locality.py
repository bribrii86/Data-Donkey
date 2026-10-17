"""Who decides: can a business buy office equipment locally, or is that decided at a franchisor, a regional office,
corporate headquarters or through public procurement? Stephanie drops corporate rows at once, so the Monday list
carries the answer and the evidence for it.

Values: local, franchise, regional, corporate, central_procurement, unknown. Each answer keeps one line of evidence,
a confidence (high, med, low), an owner hint (the likely decision maker) and where it came from.

Free tier (no API spend): name rules for public bodies, the brand lists (brands.json plus any imported Name Suggestion
Index), Overture's brand field when it agrees with the name, how many places in the area share the name, and the
NPI registry for clinics. The paid tier (runner.run_locality) sends only what is still unknown to the Console agent.
Standard library only."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .common import norm as _norm
from .ingest import connect

__all__ = ["VALUES", "CACHE_DAYS", "classify", "check_rules", "for_items", "import_brands", "key", "load_brands",
           "npi_check", "save", "stale_or_unknown"]

VALUES = ("local", "franchise", "regional", "corporate", "central_procurement", "unknown")
CONFIDENCE = ("high", "med", "low")
CACHE_DAYS = 90  # a stored answer is reused this long; only unknown answers are re-checked sooner (by the agent)
BRANDS_FILE = Path(__file__).resolve().parent / "brands.json"
NPI_URL = "https://npiregistry.cms.hhs.gov/api/"
CHAIN_COUNT = 3  # this many places in the area with the same name: a group, not one owner's shop
CORPORATE_COUNT = 8

# Public bodies buy through budgets, boards, RFPs and co-op contracts. They stay on the list (Buck 2026-10-12).
_PUBLIC = re.compile(
    r"\b(city of|town of|county|school district|unified|\busd\b|public schools?|elementary school|middle school|"
    r"high school|junior high|community college|college|university|tribal|indian community|nation|department of|"
    r"dept of|state of arizona|arizona department|fire district|fire department|fire dept|library district|"
    r"police|sheriff|justice court|superior court|municipal court|court house|courthouse|irrigation district|"
    r"water district|sanitary district|housing authority|post office|postal service|usps|federal|"
    r"social security|motor vehicle division|mvd|veterans affairs|national guard|state prison|prison|"
    r"correctional|department of corrections)\b", re.IGNORECASE)
_NOT_PUBLIC = re.compile(r"\b(credit union|bank|insurance|realty|title agency|law (office|firm)|attorneys?)\b",
                         re.IGNORECASE)
_MEDICAL = re.compile(
    r"dental|dentist|\bdds\b|\bdmd\b|orthodont|endodont|periodont|oral surgery|clinic|medical|health|urgent care|"
    r"pediatric|family practice|family medicine|physician|\bmd\b|chiropract|physical therapy|optometr|vision|eye care|"
    r"dermatolog|cardio|oncolog|hospital|imaging|radiology|laborator|pharmacy|hospice|home health|behavioral|"
    r"counseling|wellness|podiatr|audiolog|hearing|dialysis|kidney|surgery|surgical|women's center|obgyn|ob-gyn",
    re.IGNORECASE)
_OWNER_TITLE = re.compile(
    r"\b(owner|co-owner|president|ceo|partner|managing member|member|principal|dds|dmd|md|do|dc|od|founder|"
    r"proprietor|sole)\b", re.IGNORECASE)

Fetch = Callable[[str], Any]


def key(name: str, zip_code: str = "", town: str = "") -> str:
    zip5 = re.sub(r"\D", "", zip_code or "")[:5]
    return f"{_norm(name)}|{zip5 or (town or '').strip().casefold()}"


def _answer(value: str, evidence: str, confidence: str, source: str, owner_hint: str = "",
            details: dict | None = None) -> dict[str, Any]:
    return {"value": value, "evidence": evidence, "confidence": confidence, "source": source,
            "owner_hint": owner_hint, "initiative": "", "details": details or {}}


def unknown(evidence: str = "") -> dict[str, Any]:
    return _answer("unknown", evidence, "low", "rules")


def load_brands(con: sqlite3.Connection | None = None) -> dict[str, tuple[str, str]]:
    """Normalized brand name -> (kind, display name). brands.json wins over an imported list."""
    brands: dict[str, tuple[str, str]] = {}
    if con is not None:
        try:
            for row in con.execute("SELECT normalized_name, name, kind FROM brands"):
                brands[row[0]] = (row[2] if row[2] in ("corporate", "franchise") else "chain", row[1])
        except sqlite3.OperationalError:  # a read-only donkey.db from before v0.6.0
            pass
    data = json.loads(BRANDS_FILE.read_text(encoding="utf8"))
    for kind in ("corporate", "franchise"):
        for name in data.get(kind, []):
            if _norm(name):
                brands[_norm(name)] = (kind, name)
    return brands


# After a one-word brand ("Walmart Supercenter", "Shell 24"), only these words may follow; anything else means a
# different business that shares the word ("Target Pest Control", "Ace Plumbing").
_STORE_WORDS = {"store", "stores", "supercenter", "neighborhood", "market", "pharmacy", "bank", "express", "station",
                "gas", "location", "outlet", "branch", "center", "superstore", "food", "foods", "drive", "thru", "in"}


def _brand_match(name: str, brands: dict[str, tuple[str, str]], place_brand: str = "") -> tuple[str, str] | None:
    """Longest brand that starts the name on a word boundary ("Subway #1234" -> Subway). Imported (open list) brands
    and one-word brands need the rest of the name to be a store number or a store word, or Overture's brand field for
    the place to start with the same word ("Freddy's Frozen Custard" tagged Freddy's)."""
    words = _norm(name).split()
    agrees = bool(place_brand) and _norm(place_brand).split()[:1] == words[:1]
    for size in range(min(len(words), 6), 0, -1):
        hit = brands.get(" ".join(words[:size]))
        if not hit:
            continue
        rest = words[size:]
        loose = size == 1 or hit[0] == "chain"
        if loose and not agrees and any(not (w.isdigit() or w in _STORE_WORDS) for w in rest):
            continue
        return hit
    return None


def classify(name: str, category: str = "", website: str = "", brand: str = "", locations: int = 0,
             brands: dict[str, tuple[str, str]] | None = None) -> dict[str, Any]:
    """Free rules only. brand: Overture's brand field for this place (used only when it agrees with the name, since
    Overture sometimes tags a local place with an unrelated brand). locations: places in the area with this name."""
    if _PUBLIC.search(name or "") and not _NOT_PUBLIC.search(name or ""):
        return _answer("central_procurement", f"public body (name: {name})", "high", "rules")
    brands = load_brands() if brands is None else brands
    hit = _brand_match(name, brands, brand)
    if hit:
        kind, display = hit
        if kind == "corporate":
            return _answer("corporate", f"company-run chain ({display})", "high", "rules")
        if kind == "franchise":
            return _answer("franchise", f"franchise brand ({display}); ask if they choose their own equipment",
                           "med", "rules")
        return _answer("regional" if locations < CORPORATE_COUNT else "corporate",
                       f"chain brand on an open brand list ({display})", "med", "rules")
    if brand and _norm(brand) and _norm(name).startswith(_norm(brand).split()[0]):
        return _answer("regional", f"Overture lists it under the brand {brand}", "low", "rules")
    if locations >= CORPORATE_COUNT:
        return _answer("corporate", f"{locations} places in the area share this name", "med", "rules")
    if locations >= CHAIN_COUNT:
        return _answer("regional", f"{locations} places in the area share this name", "med", "rules")
    if website and re.search(r"/locations?\b|/find-a-location|/store-locator", website, re.IGNORECASE):
        return _answer("regional", f"website is a location page ({website})", "low", "rules")
    return unknown()


def looks_medical(name: str, category: str = "") -> bool:
    return bool(_MEDICAL.search(f"{name} {category}"))


def _npi_fetch(url: str) -> Any:  # pragma: no cover - network
    with urllib.request.urlopen(url, timeout=30) as response:  # nosec B310
        return json.loads(response.read().decode("utf8"))


def npi_check(name: str, fetch: Fetch | None = None) -> dict[str, Any] | None:
    """The free NPI registry for a clinic name: how many Arizona locations share the legal name, whether it is a
    subpart of a bigger organization, and the authorized official (often the owner in a small practice).
    None when the registry has no matching organization."""
    words = [w for w in re.sub(r"[^A-Za-z0-9&' ]", " ", name).split()
             if w.casefold() not in {"llc", "pllc", "pc", "inc", "ltd", "pa", "plc", "corp"}]
    if not words:
        return None
    query = urllib.parse.urlencode({"version": "2.1", "organization_name": " ".join(words) + "*", "state": "AZ",
                                    "enumeration_type": "NPI-2", "limit": "200"})
    data = (fetch or _npi_fetch)(f"{NPI_URL}?{query}")
    target = _norm(name)
    matches = [r for r in (data or {}).get("results", [])
               if _norm(r.get("basic", {}).get("organization_name", "")).startswith(target)]
    if not matches:
        return None
    places = set()
    for result in matches:
        for address in result.get("addresses", []):
            if address.get("address_purpose", "").upper() == "LOCATION" and address.get("state", "") == "AZ":
                places.add((re.sub(r"\W", "", address.get("address_1", "").casefold()),
                            str(address.get("postal_code", ""))[:5]))
    basic = matches[0].get("basic", {})
    official = " ".join(x for x in (basic.get("authorized_official_first_name", "").title(),
                                    basic.get("authorized_official_last_name", "").title()) if x)
    title = basic.get("authorized_official_title_or_position", "")
    subpart = any(r.get("basic", {}).get("organizational_subpart", "").upper() == "YES" for r in matches)
    owner_hint = f"{official}, {title} (NPI)" if official else ""
    count = len(places)
    details = {"npi": [r.get("number") for r in matches][:20], "locations": count, "subpart": subpart,
               "official": official, "title": title}
    if count >= CORPORATE_COUNT:
        return _answer("corporate", f"NPI: same legal name at {count} Arizona locations", "high", "npi", owner_hint,
                       details)
    if count >= CHAIN_COUNT or subpart:
        why = f"{count} Arizona locations" if count >= CHAIN_COUNT else "registered as part of a larger organization"
        return _answer("regional", f"NPI: {why}", "med", "npi", owner_hint, details)
    if official and _OWNER_TITLE.search(title or ""):
        return _answer("local", f"NPI: signed by {official} ({title}), {count or 1} location(s)", "med", "npi",
                       owner_hint, details)
    return _answer("unknown", "NPI: one practice, official's title doesn't say owner", "low", "npi", owner_hint,
                   details)


def save(con: sqlite3.Connection, name: str, zip_code: str, town: str, answer: dict[str, Any],
         when: str | None = None) -> None:
    if answer["value"] not in VALUES or answer.get("confidence", "low") not in CONFIDENCE:
        raise ValueError(f"bad locality answer for {name!r}: {answer['value']!r} / {answer.get('confidence')!r}")
    con.execute(
        """INSERT INTO locality (key, name, zip, town, value, evidence, confidence, owner_hint, initiative, source,
        checked_at, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET name = excluded.name, zip = excluded.zip, town = excluded.town,
        value = excluded.value, evidence = excluded.evidence, confidence = excluded.confidence,
        owner_hint = CASE WHEN excluded.owner_hint <> '' THEN excluded.owner_hint ELSE locality.owner_hint END,
        initiative = CASE WHEN excluded.initiative <> '' THEN excluded.initiative ELSE locality.initiative END,
        source = excluded.source, checked_at = excluded.checked_at, details_json = excluded.details_json""",
        (key(name, zip_code, town), name, zip_code or "", town or "", answer["value"], answer.get("evidence", ""),
         answer.get("confidence", "low"), answer.get("owner_hint", ""), answer.get("initiative", ""),
         answer.get("source", "rules"), when or datetime.now(timezone.utc).isoformat(timespec="seconds"),
         json.dumps(answer.get("details") or {}, ensure_ascii=False, sort_keys=True)))


def _stored(con: sqlite3.Connection, name: str, zip_code: str, town: str) -> dict[str, Any] | None:
    try:
        row = con.execute("SELECT * FROM locality WHERE key = ?", (key(name, zip_code, town),)).fetchone()
        if row is None and zip_code and town:
            row = con.execute("SELECT * FROM locality WHERE key = ?", (key(name, "", town),)).fetchone()
    except sqlite3.OperationalError:  # read-only donkey.db from before v0.6.0
        return None
    return dict(row) if row else None


def _fresh(row: dict[str, Any] | None, today: date) -> bool:
    if not row:
        return False
    checked = (row.get("checked_at") or "")[:10]
    return bool(checked) and checked >= (today - timedelta(days=CACHE_DAYS)).isoformat()


def stale_or_unknown(row: dict[str, Any] | None, today: date) -> bool:
    """True when the paid check should look at this business: never checked, unknown, or older than CACHE_DAYS.
    An unknown answer the agent itself gave is not re-asked until the cache runs out."""
    if not _fresh(row, today):
        return True
    return row["value"] == "unknown" and row["source"] not in ("agent", "manual")


def _name_counts(con: sqlite3.Connection) -> Counter:
    """Places per normalized name in the latest snapshot of each list (Overture and the weekly town lists)."""
    try:
        rows = con.execute(
            """SELECT snapshot_rows.name FROM snapshot_rows JOIN (SELECT label, MAX(id) AS id FROM snapshots
            GROUP BY label) latest ON latest.id = snapshot_rows.snapshot_id""").fetchall()
    except sqlite3.OperationalError:
        return Counter()
    return Counter(_norm(row[0]) for row in rows if row[0])


def _place_brand(con: sqlite3.Connection, name: str, zip_code: str) -> str:
    try:
        row = con.execute("SELECT brand FROM place_brands WHERE key = ?", (key(name, zip_code),)).fetchone()
    except sqlite3.OperationalError:
        return ""
    return row[0] if row else ""


def for_items(items: Iterable[dict], con: sqlite3.Connection, today: date | None = None) -> list[dict[str, Any]]:
    """One answer per item ({name, zip, town, category?, website?}): the stored answer when fresh, else the free
    rules on the spot (nothing written, so this works on a read-only connection)."""
    today = today or date.today()
    brands, counts = load_brands(con), _name_counts(con)
    out = []
    for item in items:
        name, zip_code, town = item.get("name", ""), item.get("zip", ""), item.get("town", "")
        row = _stored(con, name, zip_code, town)
        if _fresh(row, today):
            out.append({"value": row["value"], "evidence": row["evidence"], "confidence": row["confidence"],
                        "owner_hint": row["owner_hint"], "initiative": row["initiative"], "source": row["source"],
                        "checked_at": row["checked_at"]})
            continue
        answer = classify(name, item.get("category", ""), item.get("website", ""),
                          _place_brand(con, name, zip_code), counts.get(_norm(name), 0), brands)
        answer.pop("details", None)
        out.append({**answer, "checked_at": ""})
    return out


def check_rules(
    items: Iterable[dict],
    db_path: str | Path | None = None,
    today: date | None = None,
    npi: bool = True,
    fetch: Fetch | None = None,
    pause: float = 0.2,
) -> dict[str, Any]:
    """Free tier, stored: rules for every item not already answered (fresh), then the NPI registry for clinics the
    rules left unknown. Returns counts per value. NPI failures are notes, never errors."""
    today = today or date.today()
    con = connect(db_path)
    counts: Counter = Counter()
    notes: list[str] = []
    try:
        brands, names = load_brands(con), _name_counts(con)
        for item in items:
            name, zip_code, town = item.get("name", ""), item.get("zip", ""), item.get("town", "")
            if not _norm(name):
                continue
            row = _stored(con, name, zip_code, town)
            if _fresh(row, today) and row["value"] != "unknown":
                counts["cached"] += 1
                continue
            answer = classify(name, item.get("category", ""), item.get("website", ""),
                              _place_brand(con, name, zip_code), names.get(_norm(name), 0), brands)
            if answer["value"] == "unknown" and npi and looks_medical(name, item.get("category", "")):
                try:
                    found = npi_check(name, fetch)
                    if found:
                        answer = found
                    if pause:
                        time.sleep(pause)
                except Exception as exc:  # noqa: BLE001 - the free registry being down never stops the rules
                    notes.append(f"NPI lookup failed for {name}: {str(exc)[:120]}")
            if row and row["source"] in ("agent", "manual") and answer["value"] == "unknown":
                counts["kept"] += 1  # never overwrite a paid or hand answer with a free "unknown"
                continue
            save(con, name, zip_code, town, answer)
            counts[answer["value"]] += 1
        con.commit()
    finally:
        con.close()
    return {"counts": dict(counts), "notes": notes}


def import_brands(path: str | Path, db_path: str | Path | None = None, source: str = "name-suggestion-index") -> int:
    """Brands from the OpenStreetMap Name Suggestion Index (nsi.json from its dist folder) or any JSON with
    displayName or tags.brand entries. Brands become 'chain' (kind unknown); brands.json still decides corporate vs
    franchise for the names it lists. Returns the number stored."""
    data = json.loads(Path(path).read_text(encoding="utf8"))
    found: dict[str, tuple[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            tags = node.get("tags") if isinstance(node.get("tags"), dict) else {}
            name = tags.get("brand") or node.get("displayName")
            if isinstance(name, str) and (tags or "displayName" in node) and _norm(name):
                found.setdefault(_norm(name), (name, str(tags.get("brand:wikidata", ""))))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    con = connect(db_path)
    try:
        con.executemany("INSERT OR REPLACE INTO brands (normalized_name, name, kind, wikidata, source) "
                        "VALUES (?, ?, 'chain', ?, ?)",
                        [(k, v[0], v[1], source) for k, v in found.items() if len(k) >= 3])
        con.commit()
    finally:
        con.close()
    return len(found)
