"""Read-only queries for Data Donkey consumers."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Iterable

from stephanie_assistant.prospects import _norm

DEFAULT_DB = Path(__file__).resolve().parent.parent / "donkey.db"


def _read_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or os.environ.get("DONKEY_DB") or DEFAULT_DB).expanduser().resolve()
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def search(city: str, vertical: str, db_path: str | Path | None = None) -> list[dict]:
    """Find businesses by case-insensitive city and vertical."""
    with _read_connection(db_path) as con:
        rows = con.execute(
            """SELECT * FROM businesses
            WHERE city = ? COLLATE NOCASE AND vertical = ? COLLATE NOCASE
            ORDER BY name, id""",
            (city.strip(), vertical.strip()),
        ).fetchall()
        return [dict(row) for row in rows]


def events(
    kind: str,
    towns: Iterable[str] | str | None = None,
    since: str | None = None,
    db_path: str | Path | None = None,
) -> list[dict]:
    """Find events by kind, optional town list, and inclusive lower event-date bound."""
    town_list = [towns] if isinstance(towns, str) else list(towns or [])
    query = """SELECT events.*, businesses.name AS business_name, businesses.city AS business_city
        FROM events LEFT JOIN businesses ON businesses.id = events.business_id
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
    with _read_connection(db_path) as con:
        return [dict(row) for row in con.execute(query, params).fetchall()]


def lookup(name: str, db_path: str | Path | None = None) -> list[dict]:
    """Look up businesses with the same normalized name; callers resolve duplicates."""
    normalized = _norm(name)
    if not normalized:
        return []
    with _read_connection(db_path) as con:
        rows = con.execute(
            "SELECT * FROM businesses WHERE normalized_name = ? ORDER BY city, zip, id",
            (normalized,),
        ).fetchall()
        return [dict(row) for row in rows]
