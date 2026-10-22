"""v0.6.8: who owns the building. Pinal parcels (owner name and mailing address, year built, last sale) from the City
of Maricopa's public ArcGIS layer TaxParcel_8_26, which covers about all of Pinal County. Free, no key.

`python -m donkey parcels-pull --area stephanie` stores the non-residential parcels in the area's ZIPs (the
`parcels` table, replaced on each pull). call-this-week then gives a business at one of those addresses a `property`
entry; the owner is the building's owner, often a landlord, so only an owner whose name is like the business's (it
owns its own building) with a mailing address outside Arizona counts as not local."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import areas
from .common import database_path

LAYER = ("https://services7.arcgis.com/MlfUGd2UJYefAS7v/arcgis/rest/services/TaxParcel_8_26/FeatureServer/0/query")
USES = ("Commercial", "Industrial", "Religious", "Government", "Utility", "All")  # not homes, farms or vacant land
FIELDS = ("PARCELID", "SITEADDRES", "OWNERNME1", "OWNERNME2", "PSTLADDRES", "PSTLCITY", "PSTLSTATE", "PSTLZIP5",
          "USEDSCRP", "CLASSDSCRP", "RESYRBLT", "SALEDATE", "SALEPRICE")
PAGE = 2000  # the layer's maxRecordCount
MAX_PAGES = 50  # 100,000 parcels; the whole stephanie area is about 4,000
_STATES = {"ARIZONA": "AZ", "ARIZ": "AZ"}

SCHEMA = """CREATE TABLE IF NOT EXISTS parcels (
    parcel_id TEXT NOT NULL,
    area TEXT NOT NULL,
    site_street TEXT NOT NULL DEFAULT '',
    site_city TEXT NOT NULL DEFAULT '',
    site_zip TEXT NOT NULL DEFAULT '',
    owner TEXT NOT NULL DEFAULT '',
    owner_city TEXT NOT NULL DEFAULT '',
    owner_state TEXT NOT NULL DEFAULT '',
    use TEXT NOT NULL DEFAULT '',
    year_built INTEGER NOT NULL DEFAULT 0,
    sale_date TEXT NOT NULL DEFAULT '',
    sale_price REAL NOT NULL DEFAULT 0,
    pulled_at TEXT NOT NULL,
    PRIMARY KEY (area, parcel_id)
)"""

_SITE = re.compile(r"^(?P<rest>.*?),\s*AZ\s+(?P<zip>\d{5})", re.IGNORECASE)


def _fetch(url: str, data: dict[str, str]) -> dict[str, Any]:  # pragma: no cover - network
    body = urllib.parse.urlencode(data).encode("utf8")
    with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=120) as response:  # nosec B310
        return json.loads(response.read().decode("utf8"))


def split_site(site: str, cities: list[str]) -> tuple[str, str, str]:
    """"717 N PROMENADE PKWY CASA GRANDE, AZ 85194" -> ("717 N PROMENADE PKWY", "Casa Grande", "85194"). The town is
    taken off the end when it is one of the area's towns; otherwise the whole text before ", AZ" is the street."""
    match = _SITE.match((site or "").strip())
    if not match:
        return (site or "").strip(), "", ""
    rest = match["rest"].strip()
    for city in sorted(cities, key=len, reverse=True):
        if rest.casefold().endswith(" " + city.casefold()):
            return rest[: -len(city)].strip(), city, match["zip"]
    return rest, "", match["zip"]


def pull(area: str = "stephanie", db_path: str | Path | None = None,
         fetch: Callable[[str, dict[str, str]], dict[str, Any]] = _fetch) -> dict[str, Any]:
    """Download the area's non-residential parcels and replace that area's rows. Returns counts."""
    area_def = areas.load(area)
    zips = sorted(z for z in area_def["zips"] if z.isdigit())
    if not zips:
        raise ValueError(f"area {area!r} has no ZIP list")
    where = "(" + " OR ".join(f"SITEADDRES LIKE '%{z}'" for z in zips) + ") AND USEDSCRP IN (" + \
        ", ".join(f"'{u}'" for u in USES) + ")"
    url = os.environ.get("DONKEY_PARCELS_URL") or LAYER
    if not url.startswith("https://"):
        raise ValueError("DONKEY_PARCELS_URL must be an https:// address")
    rows, seen, offset = [], set(), 0
    for _ in range(MAX_PAGES):
        page = fetch(url, {"where": where, "outFields": ",".join(FIELDS), "returnGeometry": "false",
                           "orderByFields": "FID", "resultOffset": str(offset), "resultRecordCount": str(PAGE),
                           "f": "json"})
        if page.get("error"):
            raise RuntimeError(f"parcel layer error: {page['error'].get('message', page['error'])}")
        features = [f["attributes"] for f in page.get("features", [])]
        new = [f for f in features if str(f.get("PARCELID") or "").strip() not in seen]
        seen.update(str(f.get("PARCELID") or "").strip() for f in new)
        rows += new
        if not new or (not page.get("exceededTransferLimit") and len(features) < PAGE):
            break  # last page, or the layer sent a page we already have
        offset += len(features)
    else:
        raise RuntimeError(f"parcel layer sent more than {MAX_PAGES} pages; nothing replaced")
    now = datetime.now(timezone.utc).isoformat()
    cities = sorted({c.title() for c in area_def.get("cities", [])})
    parsed = [p for p in (_row(r, area, cities, now) for r in rows) if p]
    con = sqlite3.connect(database_path(db_path))
    try:
        con.execute(SCHEMA)
        con.execute("CREATE INDEX IF NOT EXISTS idx_parcels_zip ON parcels(site_zip)")
        (before,) = con.execute("SELECT COUNT(*) FROM parcels WHERE area = ?", (area,)).fetchone()
        if not parsed or len(parsed) < before / 2:
            raise RuntimeError(f"parcel pull got {len(parsed)} parcels (had {before}); kept the old ones")
        with con:
            con.execute("DELETE FROM parcels WHERE area = ?", (area,))
            con.executemany(
                """INSERT OR REPLACE INTO parcels (parcel_id, area, site_street, site_city, site_zip, owner,
                owner_city, owner_state, use, year_built, sale_date, sale_price, pulled_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", parsed)
    finally:
        con.close()
    return {"area": area, "parcels": len(parsed), "skipped": len(rows) - len(parsed)}


def _row(r: dict[str, Any], area: str, cities: list[str], now: str) -> tuple | None:
    """One layer record as a parcels row; None when it has no parcel ID or a number field can't be read."""
    pid = str(r.get("PARCELID") or "").strip()
    if not pid:
        return None
    try:
        year = int(float(r.get("RESYRBLT") or 0))
        price = float(r.get("SALEPRICE") or 0)
    except (TypeError, ValueError):
        return None
    street, city, zip_code = split_site(str(r.get("SITEADDRES") or ""), cities)
    owner = " ".join(str(x).strip() for x in (r.get("OWNERNME1"), r.get("OWNERNME2")) if x and str(x).strip())
    state = re.sub(r"[^A-Z]", "", str(r.get("PSTLSTATE") or "").upper())
    return (pid, area, street, city, zip_code, owner, str(r.get("PSTLCITY") or "").strip(), _STATES.get(state, state),
            str(r.get("USEDSCRP") or "").strip(), year if 1800 < year < 2200 else 0,
            str(r.get("SALEDATE") or "").strip()[:10], price, now)
