"""Growth tracking: store dated snapshots of a business list, diff two snapshots, and record the changes as
growth events with a score. Standard library only; no network calls.

A snapshot is any list of businesses with name, street, city, zip, phone, website and category columns (the
deep-researcher CSVs, a city list, an Overture export). Rows are keyed by 10-digit phone + name (doctors in one clinic share a phone), else name + ZIP."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import norm as _norm
from .ingest import connect

__all__ = ["SCORES", "diff_rows", "diff_snapshots", "is_home_based", "row_key", "take_snapshot"]

SOURCE_ID = "growth-diff"
FIELDS = ("name", "street", "city", "zip", "phone", "website", "category")

# Points per change. "call this week" on the Monday list = score >= 3 in the last 14 days.
SCORES = {
    "growth_home_to_office": 5,  # home address to a suite or commercial address: the graduation signal
    "growth_new_business": 3,
    "growth_moved": 3,
    "growth_new_website": 1,
    "growth_new_phone": 1,  # new or changed business line
    "growth_category_change": 1,
}

_SUITE = re.compile(r"\b(suite|ste|unit|bldg|building|floor|fl)\b|#\s*\w", re.I)
# Commercial arterials (John Wayne Pkwy, Florence Blvd, Pinal Ave, Maricopa Rd) are storefronts, not homes.
_ARTERIAL = re.compile(r"\b(pkwy|parkway|hwy|highway|blvd|boulevard|pinal ave|maricopa rd|main st)\b", re.I)
_PO_BOX = re.compile(r"\bp\.?\s*o\.?\s*box\b", re.I)
# Trades that usually run from a home address when the street has no suite.
_HOME_TRADES = re.compile(
    r"plumb|hvac|air cond|electric|landscap|garden|handyman|contractor|roof|paint|clean|pest|pool|"
    r"photograph|tutor|consult|mobile|taxi|towing|repair|construction|cabinet|flooring|concrete|"
    r"real estate agent|notary|bookkeep|insurance agent|salon suite|lawn|moving|janitor",
    re.I,
)


def _digits(value: str) -> str:
    digits = re.sub(r"\D", "", value or "")
    return digits[-10:] if len(digits) >= 10 else ""


def _street(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9# ]", " ", (value or "").casefold()).split())


def _name_zip(row: dict[str, str]) -> str:
    return f"name:{_norm(row.get('name', ''))}|{re.sub(r'[^0-9]', '', row.get('zip', ''))[:5]}"


def row_key(row: dict[str, str]) -> str:
    """Stable identity across snapshots: phone + name (survives a move to another ZIP), else name + ZIP."""
    phone = _digits(row.get("phone", ""))
    if phone:
        return f"phone:{phone}|{_norm(row.get('name', ''))}"
    return _name_zip(row)


def is_home_based(row: dict[str, str]) -> bool:
    """An explicit home_based column wins; else a PO box, or a home trade at a street address with no suite that is
    not on a commercial arterial. A guess: the scraper or a reviewer should fill home_based when it can."""
    explicit = str(row.get("home_based", "")).strip().casefold()
    if explicit in {"1", "true", "yes", "y"}:
        return True
    if explicit in {"0", "false", "no", "n"}:
        return False
    street = row.get("street", "")
    if _PO_BOX.search(street):
        return True
    if not street or _SUITE.search(street) or _ARTERIAL.search(street):
        return False
    return bool(_HOME_TRADES.search(row.get("category", "")))


def _clean(row: dict[str, Any]) -> dict[str, str]:
    lower = {str(k).strip().casefold(): "" if v is None else str(v).strip() for k, v in row.items()}
    out = {field: lower.get(field, "") for field in FIELDS}
    out["home_based"] = "1" if is_home_based({**out, "home_based": lower.get("home_based", "")}) else "0"
    return out


def diff_rows(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Changes from old to new, one dict per change: kind, key, score, before, after. Businesses that drop out
    are not reported: a missing row usually means the scrape missed a page, not that the business closed."""
    old_rows = list(map(_clean, old))
    before = {row_key(r): r for r in old_rows}
    by_name = {_name_zip(r): r for r in old_rows}
    changes: list[dict[str, Any]] = []
    after = {row_key(r): r for r in map(_clean, new)}  # a key listed twice keeps its last row, like a snapshot
    for key, row in after.items():
        prev = before.get(key) or by_name.get(_name_zip(row))  # name+ZIP catches a phone added or changed
        kinds: list[str] = []
        if prev is None:
            kinds.append("growth_new_business")
        else:
            moved = _street(prev["street"]) != _street(row["street"]) and bool(row["street"])
            if moved and prev["home_based"] == "1" and row["home_based"] == "0":
                kinds.append("growth_home_to_office")
            elif moved:
                kinds.append("growth_moved")
            if row["website"] and not prev["website"]:
                kinds.append("growth_new_website")
            if _digits(row["phone"]) and _digits(row["phone"]) != _digits(prev["phone"]):
                kinds.append("growth_new_phone")
            if row["category"] and prev["category"] and row["category"].casefold() != prev["category"].casefold():
                kinds.append("growth_category_change")
        for kind in kinds:
            changes.append({"kind": kind, "key": key, "score": SCORES[kind], "before": prev or {}, "after": row})
    return changes


def take_snapshot(
    file_path: str | Path, label: str, taken_at: str = "", db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Store a CSV as a dated snapshot once by SHA-256. taken_at defaults to the file's date_seen, else today."""
    path = Path(file_path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    source_rows = list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))
    rows = [_clean(r) for r in source_rows]
    if not taken_at:
        seen = sorted({(r.get("date_seen") or "").strip() for r in source_rows} - {""})
        taken_at = seen[-1] if seen else datetime.now(timezone.utc).date().isoformat()
    con = connect(db_path)
    try:
        found = con.execute("SELECT id FROM snapshots WHERE sha256 = ?", (digest,)).fetchone()
        if found:
            return {"duplicate": True, "snapshot_id": int(found["id"]), "row_count": 0}
        snap = con.execute(
            "INSERT INTO snapshots (label, taken_at, file_name, sha256, row_count) VALUES (?, ?, ?, ?, ?)",
            (label.strip(), taken_at, path.name, digest, len(rows)),
        )
        snapshot_id = int(snap.lastrowid)
        con.executemany(
            """INSERT OR REPLACE INTO snapshot_rows
            (snapshot_id, key, name, street, city, zip, phone, website, category, home_based)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(snapshot_id, row_key(r), *(r[f] for f in FIELDS), int(r["home_based"])) for r in rows],
        )
        con.commit()
        home = sum(r["home_based"] == "1" for r in rows)
        return {"duplicate": False, "snapshot_id": snapshot_id, "row_count": len(rows), "home_based": home}
    finally:
        con.close()


def _snapshot_rows(con: sqlite3.Connection, snapshot_id: int) -> list[dict[str, str]]:
    rows = con.execute("SELECT * FROM snapshot_rows WHERE snapshot_id = ?", (snapshot_id,)).fetchall()
    return [{**{f: row[f] for f in FIELDS}, "home_based": str(row["home_based"])} for row in rows]


def diff_snapshots(
    old_id: int | None = None, new_id: int | None = None, db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Diff two snapshots with the same label (default: the latest two) and store each change as an event."""
    con = connect(db_path)
    try:
        if new_id is None:
            latest = con.execute("SELECT id, label FROM snapshots ORDER BY taken_at DESC, id DESC LIMIT 1").fetchone()
            if latest is None:
                raise ValueError("no snapshots yet; run growth-snapshot first")
            new_id = int(latest["id"])
        new = con.execute("SELECT * FROM snapshots WHERE id = ?", (new_id,)).fetchone()
        if new is None:
            raise ValueError(f"no snapshot {new_id}")
        if old_id is None:
            prev = con.execute(
                """SELECT id FROM snapshots WHERE label = ? AND id != ? AND taken_at <= ?
                ORDER BY taken_at DESC, id DESC LIMIT 1""",
                (new["label"], new_id, new["taken_at"]),
            ).fetchone()
            if prev is None:
                return {"old_id": None, "new_id": new_id, "changes": 0, "by_kind": {}, "baseline": True}
            old_id = int(prev["id"])
        digest = f"diff:{old_id}:{new_id}"
        if con.execute("SELECT 1 FROM batches WHERE sha256 = ?", (digest,)).fetchone():
            return {"old_id": old_id, "new_id": new_id, "changes": 0, "by_kind": {}, "duplicate": True}
        changes = diff_rows(_snapshot_rows(con, old_id), _snapshot_rows(con, new_id))
        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT OR IGNORE INTO sources (id, name, description, created_at) VALUES (?, ?, ?, ?)",
            (SOURCE_ID, "Growth diff", "Changes between two snapshots of the same business list", now),
        )
        batch_id = int(con.execute(
            """INSERT INTO batches (source_id, file_name, sha256, received_at, received_from, row_count)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (SOURCE_ID, f"snapshots {old_id} to {new_id}", digest, now, "donkey growth-diff", len(changes)),
        ).lastrowid)
        by_kind: dict[str, int] = {}
        for number, change in enumerate(changes, start=1):
            after = change["after"]
            record_id = int(con.execute(
                """INSERT INTO records (source_id, batch_id, row_number, raw_json, business_id, match_status)
                VALUES (?, ?, ?, ?, NULL, 'new')""",
                (SOURCE_ID, batch_id, number, json.dumps(change, ensure_ascii=False, sort_keys=True)),
            ).lastrowid)
            details = {"key": change["key"], "name": after["name"], "score": change["score"],
                       "before_street": change["before"].get("street", ""), "after_street": after["street"],
                       "home_based": after["home_based"] == "1"}
            con.execute(
                """INSERT INTO events (business_id, record_id, kind, event_date, town, details_json)
                VALUES (NULL, ?, ?, ?, ?, ?)""",
                (record_id, change["kind"], new["taken_at"], after["city"], json.dumps(details, sort_keys=True)),
            )
            by_kind[change["kind"]] = by_kind.get(change["kind"], 0) + 1
        con.commit()
        return {"old_id": old_id, "new_id": new_id, "changes": len(changes), "by_kind": by_kind}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
