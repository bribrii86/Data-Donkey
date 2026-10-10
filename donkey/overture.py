"""Overture Places history: store each monthly Overture release for an area as a growth snapshot, so the diff between
releases (moves, new businesses, new websites and phones) becomes growth events. Free: public S3, no key.

Overture keeps only its latest releases on S3 (2 on 2026-10-10), so history starts with what is there and builds
as each new release is saved. Reading the Parquet files needs DuckDB, an optional extra (`pip install duckdb`); the
rest of Data Donkey stays standard library only."""

from __future__ import annotations

import csv
import re
import urllib.request
from pathlib import Path
from typing import Any, Callable

from . import areas, growth
from .common import DEFAULT_DB
from .ingest import connect

__all__ = ["releases", "fetch_rows", "snapshot_release", "snapshot_new"]

BUCKET = "overturemaps-us-west-2"
LIST_URL = f"https://{BUCKET}.s3.us-west-2.amazonaws.com/?list-type=2&prefix=release/&delimiter=/"
_RELEASE = re.compile(r"\d{4}-\d{2}-\d{2}(\.\d+)?")
COLUMNS = (*growth.FIELDS, "date_seen")

Fetcher = Callable[[str, dict], list[dict[str, str]]]


def releases(listing: str | None = None) -> list[str]:
    """Release names on S3, oldest first. When a date has several (2026-09-23.0, .1), only the latest is kept."""
    if listing is None:
        with urllib.request.urlopen(LIST_URL, timeout=60) as response:  # nosec B310
            listing = response.read().decode("utf8")
    latest: dict[str, str] = {}
    for name in re.findall(r"<Prefix>release/([^/<]+)/</Prefix>", listing):
        if _RELEASE.fullmatch(name):
            day = name[:10]
            latest[day] = max(latest.get(day, name), name, key=lambda n: int(n[11:] or 0))
    return [latest[day] for day in sorted(latest)]


def _duckdb():  # pragma: no cover - needs DuckDB and the network; run on Buck's PC
    try:
        import duckdb
    except ImportError as exc:
        raise RuntimeError("DuckDB isn't installed; run: python -m pip install duckdb") from exc
    con = duckdb.connect()
    try:  # the Sales-Pilot install ships httpfs as a pip package, so nothing is downloaded at run time
        import duckdb_extension_httpfs as ext

        con.execute(f"LOAD '{next(Path(ext.__file__).parent.glob('extensions/*/httpfs.duckdb_extension'))}'")
    except Exception:  # noqa: BLE001
        con.execute("INSTALL httpfs; LOAD httpfs;")
    con.execute("CREATE SECRET overture (TYPE s3, KEY_ID '', SECRET '', REGION 'us-west-2')")  # public bucket
    return con


def fetch_rows(release: str, area: dict) -> list[dict[str, str]]:  # pragma: no cover - network
    """One release's open places inside the area's bbox, straight from S3."""
    if not _RELEASE.fullmatch(release):
        raise ValueError(f"unexpected Overture release name {release!r}")
    box = [float(x) for x in area["bbox"]]  # numbers only go into the query
    src = f"s3://{BUCKET}/release/{release}/theme=places/type=place/*"
    sql = (
        "SELECT names.primary, addresses[1].freeform, addresses[1].locality, left(addresses[1].postcode, 5), "  # nosec B608
        "phones[1], websites[1], coalesce(basic_category, taxonomy.primary) "
        f"FROM read_parquet('{src}') "
        f"WHERE bbox.xmin BETWEEN {box[0]} AND {box[1]} AND bbox.ymin BETWEEN {box[2]} AND {box[3]} "
        "AND names.primary IS NOT NULL AND coalesce(operating_status, 'open') <> 'permanently_closed'"
    )
    keys = ("name", "street", "city", "zip", "phone", "website", "category")
    return [dict(zip(keys, ("" if v is None else str(v) for v in row))) for row in _duckdb().execute(sql).fetchall()]


def snapshot_release(
    release: str, area: str = "stephanie", fetch: Fetcher | None = None, db_path: str | Path | None = None,
    out_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Save one release for the area as CSV, store it as snapshot "overture AREA" dated by the release, and diff it
    with the previous release's snapshot."""
    area_def = areas.load(area)
    if not area_def.get("bbox"):
        raise ValueError(f"area {area!r} has no bbox in areas.json; add [lon min, lon max, lat min, lat max]")
    rows = [r for r in (fetch or fetch_rows)(release, area_def)
            if areas.contains(area_def, r.get("zip", ""), r.get("city", ""))]
    day = release[:10]
    out = Path(out_dir or DEFAULT_DB.parent / "overture") / f"overture-{release}-{area}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows({**{k: r.get(k, "") for k in growth.FIELDS}, "date_seen": day} for r in rows)
    snapshot = growth.take_snapshot(out, f"overture {area}", day, db_path=db_path)
    diff = None if snapshot["duplicate"] else growth.diff_snapshots(new_id=snapshot["snapshot_id"], db_path=db_path)
    return {"release": release, "area": area, "rows": len(rows), "csv": str(out), "snapshot": snapshot, "diff": diff}


def snapshot_new(
    area: str = "stephanie", fetch: Fetcher | None = None, listing: str | None = None,
    db_path: str | Path | None = None, out_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Every release on S3 not yet stored for this area, oldest first. Safe to run weekly: nothing new, no work."""
    con = connect(db_path)
    try:
        stored = {row["taken_at"] for row in con.execute(
            "SELECT taken_at FROM snapshots WHERE label = ?", (f"overture {area}",))}
    finally:
        con.close()
    return [snapshot_release(name, area, fetch, db_path, out_dir) for name in releases(listing)
            if name[:10] not in stored]
