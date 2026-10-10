"""Read-only queries for Data Donkey consumers."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from .common import DEFAULT_DB, database_path
from .common import norm as _norm

__all__ = ["DEFAULT_DB", "database_path", "events", "lookup", "needs_review", "search"]


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
