"""Named sales areas (Stephanie's territory, Pinal, Phoenix metro, Tucson metro, Arizona) from areas.json.
DONKEY_AREAS points to a replacement file, so a new dealer or an expansion needs no code change."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

__all__ = ["contains", "load", "names"]

DEFAULT_FILE = Path(__file__).resolve().parent / "areas.json"


def _all() -> dict[str, dict[str, Any]]:
    path = Path(os.environ.get("DONKEY_AREAS") or DEFAULT_FILE)
    data = json.loads(path.read_text(encoding="utf8"))
    return {k: v for k, v in data.items() if not k.startswith("_") and isinstance(v, dict)}


def names() -> list[str]:
    return sorted(_all())


def load(name: str) -> dict[str, Any]:
    areas = _all()
    if name not in areas:
        raise ValueError(f"unknown area {name!r}; known: {', '.join(sorted(areas))}")
    area = areas[name]
    return {
        "name": name,
        "label": area.get("label", name),
        "zips": {str(z)[:5] for z in area.get("zips", [])},
        "zip_prefixes": tuple(str(p) for p in area.get("zip_prefixes", [])),
        "cities": {str(c).casefold() for c in area.get("cities", [])},
    }


def contains(area: dict[str, Any] | str | None, zip_code: str = "", city: str = "") -> bool:
    """True when no area is given, or the ZIP or city falls in it."""
    if not area:
        return True
    if isinstance(area, str):
        area = load(area)
    zip5 = re.sub(r"\D", "", zip_code or "")[:5]
    if zip5 and (zip5 in area["zips"] or (area["zip_prefixes"] and zip5.startswith(area["zip_prefixes"]))):
        return True
    return bool(city) and city.strip().casefold() in area["cities"]
