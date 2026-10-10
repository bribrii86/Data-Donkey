"""Import source files into the shared Data Donkey SQLite store."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import DEFAULT_DB, database_path
from .common import norm as _norm
from .common import parse_date as _parse_date
from . import ucc

SOURCE_DIR = Path(__file__).resolve().parent / "sources"
__all__ = ["DEFAULT_DB", "connect", "database_path", "ingest", "ingest_overture", "load_source", "main", "unmapped_fields"]


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript((Path(__file__).parent / "schema.sql").read_text(encoding="utf8"))
    ucc.backfill(con)
    return con


def load_source(source_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", source_id):
        raise ValueError("source ID must contain lowercase letters, digits, or hyphens")
    path = SOURCE_DIR / f"{source_id}.json"
    if not path.is_file():
        raise ValueError(f"unknown source {source_id!r}; add a map at {path}")
    mapping = json.loads(path.read_text(encoding="utf8"))
    if mapping.get("id") != source_id or not mapping.get("name") or not isinstance(mapping.get("columns"), dict):
        raise ValueError(f"invalid source map: {path}")
    return mapping


def _read_rows(path: Path, raw: bytes, file_format: str) -> list[dict[str, Any]]:
    text = raw.decode("utf-8-sig")
    if file_format in {"csv", "tsv"}:
        delimiter = "\t" if file_format == "tsv" else ","
        return [dict(row) for row in csv.DictReader(io.StringIO(text), delimiter=delimiter)]
    if file_format == "json":
        payload = json.loads(text)
        if isinstance(payload, dict):
            payload = payload.get("features", payload.get("rows", [payload]))
        if not isinstance(payload, list) or any(not isinstance(row, dict) for row in payload):
            raise ValueError("JSON input must contain an array of objects")
        return payload
    if file_format == "jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in rows):
            raise ValueError("JSONL input must contain one object per line")
        return rows
    raise ValueError(f"unsupported source format {file_format!r} for {path.name}")


def _get_path(row: dict[str, Any], key: str) -> Any:
    if key in row:
        return row[key]
    value: Any = row
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _mapped_values(row: dict[str, Any], columns: dict[str, Any]) -> dict[str, str]:
    by_lower = {str(k).strip().casefold(): v for k, v in row.items()}
    output: dict[str, str] = {}
    for field, specification in columns.items():
        keys = specification if isinstance(specification, list) else [specification]
        for key in keys:
            if not isinstance(key, str):
                continue
            value = _get_path(row, key)
            if value is None:
                value = by_lower.get(key.strip().casefold())
            if value is not None and not isinstance(value, (dict, list)):
                output[field] = str(value).strip()
                break
        output.setdefault(field, "")
    return output


def unmapped_fields(rows: list[dict[str, Any]], columns: dict[str, Any]) -> list[str]:
    """Mapped fields whose column names appear nowhere in the file's header: add the real names to the map."""
    header = {str(k).strip().casefold() for k in (rows[0] if rows else {})}
    missing = []
    for field, specification in columns.items():
        keys = specification if isinstance(specification, list) else [specification]
        if not any(isinstance(k, str) and (k.strip().casefold() in header or "." in k) for k in keys):
            missing.append(field)
    return missing


def _street_norm(value: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", value.casefold()).split())


def _iso_date(value: str) -> str:
    parsed = _parse_date(value)
    return parsed.isoformat() if parsed else ""


def _find_business(con: sqlite3.Connection, values: dict[str, str]) -> tuple[int | None, str, list[int]]:
    name = values.get("name", "").strip()
    normalized = _norm(name) if name else ""
    if not normalized:
        return None, "needs_review", []
    same_name = con.execute(
        "SELECT id, zip, street FROM businesses WHERE normalized_name = ? ORDER BY id",
        (normalized,),
    ).fetchall()
    zip_code = re.sub(r"\D", "", values.get("zip", ""))
    street = _street_norm(values.get("street", "") or values.get("address", ""))
    location_matches = [
        row for row in same_name
        if (zip_code and re.sub(r"\D", "", row["zip"]) == zip_code)
        or (street and _street_norm(row["street"]) == street)
    ]
    if len(location_matches) == 1:
        return int(location_matches[0]["id"]), "matched", []
    if len(location_matches) > 1:
        return None, "needs_review", [int(row["id"]) for row in location_matches]
    if same_name and not (zip_code or street):
        if len(same_name) == 1:
            return int(same_name[0]["id"]), "matched", []
        return None, "needs_review", [int(row["id"]) for row in same_name]

    now = datetime.now(timezone.utc).isoformat()
    cursor = con.execute(
        """INSERT INTO businesses
        (name, normalized_name, address, street, city, state, zip, vertical,
         license_number, open_date, naics, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            name, normalized, values.get("address", ""), values.get("street", "") or values.get("address", ""),
            values.get("city", ""), values.get("state", ""), values.get("zip", ""),
            values.get("vertical", ""), values.get("license_number", ""), values.get("open_date", ""),
            values.get("naics", ""), now, now,
        ),
    )
    return int(cursor.lastrowid), "new", []


def ingest(
    source_id: str,
    file_path: str | Path,
    received_from: str,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Ingest a mapped CSV, JSON array, or JSONL file once by SHA-256."""
    mapping = load_source(source_id)
    path = Path(file_path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    rows = _read_rows(path, raw, mapping.get("format", path.suffix.lstrip(".").lower()))
    return _ingest_rows(mapping, rows, path.name, digest, received_from, db_path)


def _ingest_rows(
    mapping: dict[str, Any],
    rows: list[dict[str, Any]],
    file_name: str,
    digest: str,
    received_from: str,
    db_path: str | Path | None,
) -> dict[str, Any]:
    source_id = mapping["id"]
    con = connect(db_path)
    try:
        if con.execute("SELECT 1 FROM batches WHERE sha256 = ?", (digest,)).fetchone():
            return {"duplicate": True, "new": 0, "matched": 0, "needs_review": 0, "row_count": 0}

        now = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT OR IGNORE INTO sources (id, name, description, created_at) VALUES (?, ?, ?, ?)",
            (source_id, mapping["name"], mapping.get("description", ""), now),
        )
        batch = con.execute(
            """INSERT INTO batches (source_id, file_name, sha256, received_at, received_from, row_count)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (source_id, file_name, digest, now, received_from.strip(), len(rows)),
        )
        batch_id = int(batch.lastrowid)
        counts = {"new": 0, "matched": 0, "needs_review": 0}
        event_map = mapping.get("event") or {}
        contact_fields = {
            "name": "contact_name", "title": "contact_title", "email": "contact_email", "phone": "contact_phone",
        }
        for row_number, row in enumerate(rows, start=1):
            values = _mapped_values(row, mapping["columns"])
            for date_field in ("event_date", "open_date"):
                if values.get(date_field):
                    values[date_field] = _iso_date(values[date_field])
            business_id, status, review_candidates = _find_business(con, values)
            counts[status] += 1
            record = con.execute(
                """INSERT INTO records
                (source_id, batch_id, row_number, raw_json, business_id, match_status, review_candidates_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id, batch_id, row_number, json.dumps(row, ensure_ascii=False, sort_keys=True),
                    business_id, status, json.dumps(review_candidates),
                ),
            )
            record_id = int(record.lastrowid)
            kind = event_map.get("kind")
            if kind:
                details = {key: values.get(key, "") for key in event_map.get("details", [])}
                if mapping.get("enrich") == "ucc":
                    details.update(ucc.enrich(values))
                con.execute(
                    """INSERT INTO events (business_id, record_id, kind, event_date, town, details_json)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        business_id, record_id, kind, values.get(event_map.get("date", ""), ""),
                        values.get(event_map.get("town", ""), ""),
                        json.dumps(details, ensure_ascii=False, sort_keys=True),
                    ),
                )
            contact = {key: values.get(value, "") for key, value in contact_fields.items()}
            if any(contact.values()):
                con.execute(
                    """INSERT INTO contacts (business_id, record_id, name, title, email, phone)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (business_id, record_id, contact["name"], contact["title"], contact["email"], contact["phone"]),
                )
        con.commit()
        return {"duplicate": False, **counts, "row_count": len(rows), "batch_id": batch_id, "sha256": digest,
                "unmapped": unmapped_fields(rows, mapping["columns"])}
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def ingest_overture(
    history_db: str | Path | None = None,
    received_from: str = "Stephanie app history.db",
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    """Copy history.db's overture_places table into Data Donkey once per distinct table contents."""
    history_db = history_db or os.environ.get("STEPHANIE_DB")
    if not history_db:
        raise ValueError("no history database given; pass --history-db PATH or set STEPHANIE_DB")
    path = Path(history_db).expanduser()
    if not path.is_file():
        raise ValueError(f"history database not found: {path}")
    src = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in src.execute("SELECT * FROM overture_places ORDER BY id")]
    except sqlite3.Error as exc:
        raise ValueError(f"{path} has no readable overture_places table: {exc}") from exc
    finally:
        src.close()
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode("utf8")).hexdigest()
    return _ingest_rows(load_source("overture-history-db"), rows, f"{path.name}:overture_places", digest,
                        received_from, db_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m donkey")
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("ingest", help="ingest a mapped source file")
    command.add_argument("--source", required=True, help="source map ID")
    command.add_argument("--file", required=True, help="path to source file")
    command.add_argument("--received-from", required=True, help="person or organization who supplied the file")
    overture = commands.add_parser("ingest-overture", help="copy overture_places from the app's history.db")
    overture.add_argument("--history-db", help="path to the app's history.db (default: STEPHANIE_DB)")
    overture.add_argument("--received-from", default="Stephanie app history.db")
    snap = commands.add_parser("growth-snapshot", help="store a dated copy of a business list CSV")
    snap.add_argument("--file", required=True, help="CSV with name, street, city, zip, phone, website, category")
    snap.add_argument("--label", required=True, help="list name; snapshots with the same label are diffed")
    snap.add_argument("--taken-at", default="", help="YYYY-MM-DD (default: the file's date_seen, else today)")
    snap.add_argument("--area", help="keep only rows in this area (see areas.json), e.g. stephanie, phoenix-metro")
    calls = commands.add_parser("call-this-week", help="rank businesses to call this week in an area")
    calls.add_argument("--area", help="area from areas.json (default: all)")
    calls.add_argument("--as-of", help="YYYY-MM-DD (default: today)")
    calls.add_argument("--days", type=int, default=14, help="look back this many days (default 14)")
    calls.add_argument("--min-score", type=int, default=3)
    calls.add_argument("--json", action="store_true", help="print JSON instead of a table")
    diff = commands.add_parser("growth-diff", help="turn changes between the latest two snapshots into events")
    diff.add_argument("--old", type=int, help="older snapshot ID (default: previous with the same label)")
    diff.add_argument("--new", type=int, help="newer snapshot ID (default: latest)")
    weekly = commands.add_parser("growth-weekly", help="paid: run the Console agent for one town, then snapshot+diff")
    weekly.add_argument("--town", help="town to run (default: next in the area's rotation)")
    weekly.add_argument("--dry-run", action="store_true", help="show town, budget and prompt; no API calls")
    weekly.add_argument("--install-task", action="store_true", help="register the weekly Windows scheduled task")
    weekly.add_argument("--day", default="SUN", help="task day, MON to SUN (default SUN)")
    weekly.add_argument("--time", default="03:00", help="task time, HH:MM 24-hour (default 03:00)")
    args = parser.parse_args(argv)
    if args.command == "growth-weekly":
        from . import runner

        try:
            if args.install_task:
                return runner.install_task(args.day, args.time, args.dry_run)
            result = runner.run_weekly(runner.load_settings(), town=args.town, dry_run=args.dry_run)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"growth-weekly failed: {exc}", file=sys.stderr)
            return 1
        if result["status"] == "dry_run":
            print(f"town: {result['town']} ({result['area']}); this month: ${result['month_spent_cents'] / 100:.2f} "
                  f"spent; this run's cap: ${result['budget_cents'] / 100:.2f}\n\n{result['prompt']}")
        elif result["status"] == "skipped_cap":
            print(f"Skipped {result['town']}: monthly cap reached (${result['month_spent_cents'] / 100:.2f} spent).")
        else:
            diff = result.get("diff") or {}
            print(f"{result['town']}: {result['status']}, ${result['cost_cents'] / 100:.2f}, {result['rows']} rows "
                  f"from {result['files']} files, {diff.get('changes', 0)} growth changes. CSV: {result['csv']}")
        return 0
    if args.command == "call-this-week":
        from .triggers import call_this_week

        try:
            hits = call_this_week(args.area, args.as_of, args.days, args.min_score)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"call-this-week failed: {exc}", file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(hits, indent=2))
        else:
            for hit in hits:
                where = hit["town"] or hit["zip"] or "location unknown"
                print(f"{hit['score']:>3}  {hit['name']} ({where}): {'; '.join(hit['reasons'])}")
            print(f"{len(hits)} to call")
        return 0
    if args.command.startswith("growth-"):
        from . import growth  # imported here: growth imports this module

        try:
            if args.command == "growth-snapshot":
                result = growth.take_snapshot(args.file, args.label, args.taken_at, area=args.area)
                print("Already stored; no changes made." if result["duplicate"] else
                      f"snapshot {result['snapshot_id']}: {result['row_count']} rows, "
                      f"{result['home_based']} look home-based")
            else:
                result = growth.diff_snapshots(args.old, args.new)
                if result.get("baseline"):
                    print("First snapshot for this list; nothing to compare yet.")
                elif result.get("duplicate"):
                    print("Already diffed; no changes made.")
                else:
                    kinds = ", ".join(f"{k.removeprefix('growth_')}: {v}" for k, v in sorted(result["by_kind"].items()))
                    print(f"snapshots {result['old_id']} to {result['new_id']}: {result['changes']} changes ({kinds})")
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"growth failed: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        if args.command == "ingest-overture":
            result = ingest_overture(args.history_db, args.received_from)
        else:
            result = ingest(args.source, args.file, args.received_from)
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 1
    if result["duplicate"]:
        print("Already ingested; no changes made.")
    else:
        print(f"new: {result['new']}; matched: {result['matched']}; needs-review: {result['needs_review']}")
        if result.get("unmapped"):
            print(f"not found in this file (add its column names to the source map): {', '.join(result['unmapped'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
