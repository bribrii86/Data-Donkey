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


def _find_business(
    con: sqlite3.Connection, values: dict[str, str], by_city: bool = False,
) -> tuple[int | None, str, list[int]]:
    """by_city: a file with a town but no street or ZIP (Crustdata) only matches a same-name business in that town."""
    name = values.get("name", "").strip()
    normalized = _norm(name) if name else ""
    if not normalized:
        return None, "needs_review", []
    same_name = con.execute(
        "SELECT id, zip, street, city FROM businesses WHERE normalized_name = ? ORDER BY id",
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
    if by_city and not (zip_code or street):
        city = values.get("city", "").strip().casefold()
        same_name = [row for row in same_name if city and (row["city"] or "").strip().casefold() == city]
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
    missing = [f for f in mapping.get("required", []) if f in unmapped_fields(rows, mapping["columns"])]
    if rows and missing:  # refuse before recording the batch, so the file can be ingested again after a map fix
        raise ValueError(f"{file_name}: no column found for {', '.join(missing)}; add this file's header names to "
                         f"donkey/sources/{source_id}.json")
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
        keep = {field: {str(v).strip().casefold() for v in allowed} for field, allowed in (mapping.get("keep") or {}).items()}
        skipped = 0
        for row_number, row in enumerate(rows, start=1):
            values = _mapped_values(row, mapping["columns"])
            if mapping.get("prepare") == "crustdata":  # "Casa Grande, Arizona, United States"; dated the import day
                parts = [part.strip() for part in values.get("headquarters", "").split(",")]
                values["city"] = parts[0].title() if parts else ""
                values["state"] = parts[1] if len(parts) > 1 else ""
                values["event_date"] = now[:10]
            if any(str(values.get(field, "")).strip().casefold() not in allowed for field, allowed in keep.items()):
                skipped += 1  # e.g. a national file where only Arizona rows are wanted
                continue
            for date_field in ("event_date", "open_date"):
                if values.get(date_field):
                    values[date_field] = _iso_date(values[date_field])
            business_id, status, review_candidates = _find_business(con, values,
                                                                    by_city=mapping.get("prepare") == "crustdata")
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
        if rows and keep and skipped == len(rows):  # rolled back below, so the file isn't marked as ingested
            raise ValueError(f"{file_name}: the map's filter ({', '.join(keep)}) kept 0 of {len(rows)} rows; nothing "
                             "stored. Check the file and its column names.")
        con.commit()
        return {"duplicate": False, **counts, "row_count": len(rows), "skipped": skipped, "batch_id": batch_id,
                "sha256": digest,
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


def _overture_line(result: dict[str, Any]) -> str:
    diff = result.get("diff") or {}
    if diff.get("baseline"):
        change = "first release stored; changes start with the next one"
    else:
        kinds = ", ".join(f"{k.removeprefix('growth_')}: {v}" for k, v in sorted(diff.get("by_kind", {}).items()))
        change = f"{diff.get('changes', 0)} changes ({kinds or 'none'})"
    return f"Overture {result['release']} ({result['area']}): {result['rows']} places; {change}"


def _print_ab(result: dict[str, Any]) -> None:
    agent = result.get("agent") or {}
    print(f"{result['town']}: {result['pages']} pages ({result['pages_skipped']} skipped), "
          f"{result['html_chars']:,} HTML chars -> {result['markdown_chars']:,} Markdown chars, model {result['model']}")
    print(f"Agent run {agent.get('started_at') or 'none found'}: ${(agent.get('cost_cents') or 0) / 100:.2f}, "
          f"{agent.get('rows') or 0} rows" + (f", usage {agent['usage']}" if agent.get("usage") else ""))
    if result["status"] == "dry_run":
        print(f"Dry run: worst case ${result['worst_case_cents'] / 100:.2f}, budget ${result['budget_cents'] / 100:.2f}")
        for page in result["page_list"]:
            print(f"  {page['status']} {page['markdown_chars']:>8,} {page['url']} {page['skipped']}")
        return
    if result["status"] == "skipped_cap":
        print("Skipped: monthly cap reached")
        return
    crawl, cmp = result["crawl"], result["compare"]
    print(f"Crawl4AI: ${crawl['cost_cents'] / 100:.2f}, {crawl['calls']} calls, {crawl['input_tokens']:,} in / "
          f"{crawl['output_tokens']:,} out tokens, {crawl['rows']} rows" + (f"; {crawl['stopped']}" if crawl["stopped"] else ""))
    print(f"Businesses: agent {cmp['agent_businesses']}, crawl {cmp['crawl_businesses']}, both {cmp['both']}, "
          f"only agent {cmp['only_agent']}, only crawl {cmp['only_crawl']}")
    print(f"With phone: agent {cmp['agent_with_phone']}, crawl {cmp['crawl_with_phone']}. "
          f"With street: agent {cmp['agent_with_street']}, crawl {cmp['crawl_with_street']}")
    print(f"Report: {result['report']}  CSV: {result['csv']}")


def _print_weekly(result: dict[str, Any]) -> None:
    if result["status"] in ("nothing_to_check", "no_bodies"):
        free = (result.get("free") or {}).get("counts")
        print(f"{result['town']}: nothing for the paid check" + (f" (free rules: {free})" if free else "")
              + (f"; {result['note']}" if result.get("note") else ""))
    elif result["status"] == "dry_run":
        print(f"job: {result['town']} ({result['area']}); this month: ${result['month_spent_cents'] / 100:.2f} "
              f"spent; this run's cap: ${result['budget_cents'] / 100:.2f}\n\n{result['prompt']}\n")
    elif result["status"] == "skipped_cap":
        print(f"Skipped {result['town']}: monthly cap reached (${result['month_spent_cents'] / 100:.2f} spent).")
    elif result["town"] in ("who decides", "board minutes"):
        what = "who-decides answers" if result["town"] == "who decides" else "relevant meeting documents"
        print(f"{result['town']}: {result['status']}, ${result['cost_cents'] / 100:.2f}, {result['rows']} {what}. "
              f"CSV: {result.get('csv', '')}")
    elif "diff" in result:
        diff = result.get("diff") or {}
        print(f"{result['town']}: {result['status']}, ${result['cost_cents'] / 100:.2f}, {result['rows']} rows "
              f"from {result['files']} files, {diff.get('changes', 0)} growth changes. CSV: {result['csv']}")
    else:
        print(f"{result['town']}: {result['status']}, ${result['cost_cents'] / 100:.2f}, {result['rows']} new "
              f"formation notices from {result['files']} files. CSV: {result['csv']}")


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
    lease = commands.add_parser("lease-calendar", help="free: copier leases likely ending, by month (UCC estimates)")
    lease.add_argument("--area", default="stephanie", help="area from areas.json")
    lease.add_argument("--as-of", help="YYYY-MM-DD (default: today)")
    lease.add_argument("--months", type=int, default=12, help="months ahead (default 12)")
    lease.add_argument("--all-lessors", action="store_true", help="include equipment-finance leases, not just copier")
    lease.add_argument("--csv", help="also write the rows to this CSV file")
    diff = commands.add_parser("growth-diff", help="turn changes between the latest two snapshots into events")
    diff.add_argument("--old", type=int, help="older snapshot ID (default: previous with the same label)")
    diff.add_argument("--new", type=int, help="newer snapshot ID (default: latest)")
    diff.add_argument("--redo-all", action="store_true", help="recompute every stored diff with the current rules")
    weekly = commands.add_parser("growth-weekly", help="paid: run the Console agent for one town, then snapshot+diff")
    weekly.add_argument("--town", help="town to run (default: next in the area's rotation)")
    weekly.add_argument("--dry-run", action="store_true", help="show town, budget and prompt; no API calls")
    weekly.add_argument("--install-task", action="store_true", help="register the weekly Windows scheduled task")
    weekly.add_argument("--day", default="SUN", help="task day, MON to SUN (default SUN)")
    weekly.add_argument("--time", default="03:00", help="task time, HH:MM 24-hour (default 03:00)")
    weekly.add_argument("--no-notices", action="store_true", help="skip the Pinal formation notices job")
    weekly.add_argument("--notices-only", action="store_true", help="run only the Pinal formation notices job")
    weekly.add_argument("--no-minutes", action="store_true", help="skip the board-minutes job")
    weekly.add_argument("--no-locality", action="store_true", help="skip the paid who-decides check")
    who = commands.add_parser("who-decides", help="free: who decides (local, franchise, corporate...) for this "
                                                  "week's call-this-week businesses, stored in donkey.db")
    who.add_argument("--area", default="stephanie", help="area from areas.json")
    who.add_argument("--no-npi", action="store_true", help="skip the free NPI registry lookups for clinics")
    brands = commands.add_parser("import-brands", help="free: import chain brand names (Name Suggestion Index JSON)")
    brands.add_argument("--file", required=True, help="nsi.json from github.com/osmlab/name-suggestion-index dist/")
    ab = commands.add_parser("crawl-ab", help="paid, capped: Crawl4AI fetch + Claude extraction on one town's pages, "
                                              "compared with the agent's last run (weekly job unchanged)")
    ab.add_argument("--town", required=True, help="town the agent already ran, e.g. Maricopa")
    ab.add_argument("--urls", help="file of page URLs, one per line (default: the agent run's source_url pages)")
    ab.add_argument("--dry-run", action="store_true", help="fetch pages (free) and show sizes and worst-case cost")
    ab.add_argument("--agent-usage", action="store_true", help="also ask the Console for the agent session's tokens")
    ovt = commands.add_parser("overture-snapshot", help="free: store Overture releases for an area and diff them")
    ovt.add_argument("--area", default="stephanie", help="area from areas.json (needs a bbox)")
    ovt.add_argument("--release", help="one release, e.g. 2026-09-23.0 (default: every release not yet stored)")
    args = parser.parse_args(argv)
    if args.command == "overture-snapshot":
        from . import overture

        try:
            results = ([overture.snapshot_release(args.release, args.area)] if args.release
                       else overture.snapshot_new(args.area))
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"overture-snapshot failed: {exc}", file=sys.stderr)
            return 1
        for result in results:
            print(_overture_line(result))
        if not results:
            print("No new Overture release; nothing to do.")
        return 0
    if args.command == "crawl-ab":
        from . import crawl, runner

        try:
            settings = runner.load_settings()
            console = (runner.ConsoleClient(settings["api_key"], settings["home"] / "logs" / "console-calls.jsonl")
                       if args.agent_usage else None)
            result = crawl.run_ab(settings, args.town, args.urls, args.dry_run, console=console)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"crawl-ab failed: {exc}", file=sys.stderr)
            return 1
        _print_ab(result)
        return 0
    if args.command == "growth-weekly":
        from . import runner

        try:
            if args.install_task:
                return runner.install_task(args.day, args.time, args.dry_run)
            settings = runner.load_settings()
            if not args.dry_run:  # free monthly Overture check first, so a paid-run problem never blocks it
                from . import overture

                try:
                    for done in overture.snapshot_new(settings["area"]):
                        print(_overture_line(done))
                except Exception as exc:  # noqa: BLE001 - optional (DuckDB, network); the paid run still goes
                    print(f"Overture check skipped: {exc}", file=sys.stderr)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            print(f"growth-weekly failed: {exc}", file=sys.stderr)
            return 1
        jobs = ([] if args.notices_only else [lambda: runner.run_weekly(settings, town=args.town, dry_run=args.dry_run)])
        jobs += [] if args.no_notices else [lambda: runner.run_notices(settings, dry_run=args.dry_run)]
        if not args.notices_only:  # who-decides last, so this week's new rows get checked
            jobs += [] if args.no_minutes else [lambda: runner.run_minutes(settings, dry_run=args.dry_run)]
            jobs += [] if args.no_locality else [lambda: runner.run_locality(settings, dry_run=args.dry_run)]
        failed = 0
        for job in jobs:  # one job failing never stops the other
            try:
                _print_weekly(job())
            except Exception as exc:  # noqa: BLE001 - one job's failure, of any kind, never stops the other
                print(f"growth-weekly failed: {exc}", file=sys.stderr)
                failed = 1
        return failed
    if args.command in ("who-decides", "import-brands"):
        from . import locality
        from .triggers import call_this_week

        try:
            if args.command == "import-brands":
                print(f"{locality.import_brands(args.file)} brands stored")
                return 0
            connect().close()
            hits = call_this_week(args.area, days=14, min_score=0)
            done = locality.check_rules(hits, npi=not args.no_npi)
            for note in done["notes"]:
                print(note, file=sys.stderr)
            for hit in call_this_week(args.area, days=14, min_score=0):
                answer = hit["locality"]
                print(f"{answer['value']:<20} {hit['name']} ({hit['town'] or hit['zip']}): {answer['evidence']}"
                      + (f" | owner: {answer['owner_hint']}" if answer["owner_hint"] else ""))
            print(f"stored: {done['counts']}")
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"{args.command} failed: {exc}", file=sys.stderr)
            return 1
        return 0
    if args.command == "lease-calendar":
        from .triggers import lease_calendar

        try:
            rows = lease_calendar(args.area, args.as_of, args.months, args.all_lessors)
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"lease-calendar failed: {exc}", file=sys.stderr)
            return 1
        month = ""
        for row in rows:
            if row["month"] != month:
                month = row["month"]
                print(f"\n{month}")
            where = row["town"] or row["zip"] or "location unknown"
            who = "" if row["who_decides"] == "unknown" else f" [who decides: {row['who_decides']}]"
            print(f"  {row['lease_end_est']}  {row['name']} ({where}), {row['secured_party']}, filed {row['filed']}{who}")
        print(f"{len(rows)} leases likely ending")
        if args.csv and rows:
            with open(args.csv, "w", encoding="utf8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            print(f"CSV: {args.csv}")
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
                who = hit["locality"]["value"]
                print(f"{hit['score']:>3}  {hit['name']} ({where}): {'; '.join(hit['reasons'])}"
                      + ("" if who == "unknown" else f" [who decides: {who}]"))
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
            elif args.redo_all:
                for result in growth.redo_diffs():
                    print(f"snapshots {result['old_id']} to {result['new_id']}: {result['changes']} changes")
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
        print(f"new: {result['new']}; matched: {result['matched']}; needs-review: {result['needs_review']}"
              + (f"; left out by the map's filter: {result['skipped']}" if result.get("skipped") else ""))
        if result.get("unmapped"):
            print(f"not found in this file (add its column names to the source map): {', '.join(result['unmapped'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
