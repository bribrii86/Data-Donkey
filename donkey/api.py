"""Read-only queries for Data Donkey consumers."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from . import areas as _areas
from .common import DEFAULT_DB, database_path
from .common import norm as _norm

__all__ = ["DEFAULT_DB", "database_path", "call_this_week", "events", "growth", "lookup", "needs_review", "search"]


def _read_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = database_path(db_path)
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def search(city: str, vertical: str, db_path: str | Path | None = None) -> list[dict]:
    """Find businesses by case-insensitive city and vertical."""
    con = _read_connection(db_path)
    try:
        rows = con.execute(
            """SELECT * FROM businesses
            WHERE city = ? COLLATE NOCASE AND vertical = ? COLLATE NOCASE
            ORDER BY name, id""",
            (city.strip(), vertical.strip()),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def events(
    kind: str,
    towns: Iterable[str] | str | None = None,
    since: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Find events by kind, optional town list, and inclusive lower event-date bound."""
    town_list = [towns] if isinstance(towns, str) else list(towns or [])
    query = """SELECT events.*, businesses.name AS business_name, businesses.city AS business_city,
        records.raw_json AS record_json
        FROM events JOIN records ON records.id = events.record_id LEFT JOIN businesses ON businesses.id = events.business_id
        WHERE events.kind = ?"""
    params: list[str] = [kind]
    if town_list:
        marks = ",".join("?" for _ in town_list)
        query += f" AND (events.town COLLATE NOCASE IN ({marks}) OR businesses.city COLLATE NOCASE IN ({marks}))"
        params.extend(town_list)
        params.extend(town_list)
    if since:
        query += " AND events.event_date >= ?"
        params.append(since)
    query += " ORDER BY events.event_date, events.id"
    con = _read_connection(db_path)
    try:
        return [dict(row) for row in con.execute(query, params).fetchall()]
    finally:
        con.close()


def lookup(name: str, db_path: str | Path | None = None) -> list[dict]:
    """Look up businesses with the same normalized name; callers resolve duplicates."""
    normalized = _norm(name)
    if not normalized:
        return []
    con = _read_connection(db_path)
    try:
        rows = con.execute(
            "SELECT * FROM businesses WHERE normalized_name = ? ORDER BY city, zip, id",
            (normalized,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def needs_review(db_path: str | Path | None = None) -> list[dict]:
    """List source rows that could not be matched without risking an incorrect merge."""
    con = _read_connection(db_path)
    try:
        rows = con.execute(
            """SELECT records.*, sources.name AS source_name, batches.file_name,
            batches.received_at FROM records
            JOIN sources ON sources.id = records.source_id
            JOIN batches ON batches.id = records.batch_id
            WHERE records.match_status = 'needs_review'
            ORDER BY batches.received_at, records.row_number"""
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def growth(
    towns: Iterable[str] | str | None = None,
    since: str | None = None,
    min_score: int = 3,
    db_path: str | Path | None = None,
    area: str | None = None,
) -> list[dict]:
    """Businesses whose growth_* events since a date add up to min_score or more, highest first.
    Filter by town list and/or a named area from areas.json (stephanie, pinal, phoenix-metro, tucson-metro, arizona).
    The Monday list shows these as "call this week"."""
    area_def = _areas.load(area) if area else None
    town_list = {t.casefold() for t in ([towns] if isinstance(towns, str) else list(towns or []))}
    query = "SELECT * FROM events WHERE kind LIKE 'growth\\_%' ESCAPE '\\'"
    params: list[str] = []
    if since:
        query += " AND event_date >= ?"
        params.append(since)
    con = _read_connection(db_path)
    try:
        rows = con.execute(query + " ORDER BY event_date, id", params).fetchall()
    finally:
        con.close()
    totals: dict[str, dict] = {}
    for row in rows:
        if town_list and row["town"].casefold() not in town_list:
            continue
        details = json.loads(row["details_json"])
        if area_def and not _areas.contains(area_def, details.get("zip", ""), row["town"]):
            continue
        item = totals.setdefault(details["key"], {
            "key": details["key"], "name": details["name"], "town": row["town"], "score": 0, "signals": [],
            "street": details["after_street"], "zip": details.get("zip", ""), "home_based": details["home_based"],
            "last_date": "",
        })
        item["score"] += int(details["score"])
        item["signals"].append(row["kind"].removeprefix("growth_"))
        item["last_date"] = max(item["last_date"], row["event_date"])
    hits = [item for item in totals.values() if item["score"] >= min_score]
    return sorted(hits, key=lambda item: (-item["score"], item["name"]))


def call_this_week(*args, **kwargs) -> list[dict]:
    """See donkey.triggers.call_this_week."""
    from .triggers import call_this_week as _call

    return _call(*args, **kwargs)
