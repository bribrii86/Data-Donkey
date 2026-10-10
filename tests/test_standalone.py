"""v0.5.0: Data Donkey stands alone (no Stephanie app import), with the overture copy and default paths."""
import sqlite3
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from donkey import api, common
from donkey.ingest import ingest_overture, main

ROOT = Path(__file__).resolve().parent.parent

OVERTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS overture_places (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, vertical TEXT, category TEXT,
  street TEXT, city TEXT, zip TEXT, phone TEXT, website TEXT,
  status TEXT, confidence REAL, lon REAL, lat REAL
);
"""


def test_no_app_import():
    code = "import sys, donkey.api, donkey.ingest; sys.exit('stephanie_assistant' in sys.modules)"
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0
    for path in (ROOT / "donkey").rglob("*.py"):
        assert "stephanie_assistant" not in path.read_text(encoding="utf8"), path


@pytest.mark.parametrize("raw, want", [
    ("Copper Mesa Dental, PLLC", "copper mesa dental"),
    ("Banner Urgent Care | Casa Grande, AZ | McMurray Blvd", "banner urgent care"),
    ("Arizona City Med Ctr Inc.", "arizonacity medical center"),
    ("O'Brien's Svc Co", "obriens services"),
])
def test_norm_matches_the_app(raw, want):
    assert common.norm(raw) == want


@pytest.mark.parametrize("raw, want", [
    ("2024-03-12", date(2024, 3, 12)), ("03/12/2024", date(2024, 3, 12)), ("3/12/24", date(2024, 3, 12)),
    ("20240312", date(2024, 3, 12)), ("12-Mar-2024", date(2024, 3, 12)), ("Mar 12, 2024", date(2024, 3, 12)),
    ("not a date", None),
])
def test_parse_date(raw, want):
    assert common.parse_date(raw) == want


def test_default_db_is_per_user_and_env_overrides(monkeypatch, tmp_path):
    monkeypatch.delenv("DONKEY_DB", raising=False)
    assert common.database_path() == (Path.home() / "DataDonkey" / "donkey.db").resolve()
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "x.db"))
    assert common.database_path() == (tmp_path / "x.db").resolve()
    assert common.database_path(tmp_path / "y.db") == (tmp_path / "y.db").resolve()


def _history(path, n=2):
    con = sqlite3.connect(path)
    con.executescript(OVERTURE_SCHEMA)
    for i in range(n):
        con.execute("INSERT INTO overture_places VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"p{i}", f"Made Up Cafe {i}", "restaurant", "cafe", f"{i} Test St", "Casa Grande", "85122",
                     "", "", "open", 0.9, -111.7, 32.8))
    con.commit()
    con.close()


def test_overture_copy_from_fake_history(tmp_path):
    hist, db = tmp_path / "history.db", tmp_path / "donkey.db"
    _history(hist)
    first = ingest_overture(hist, db_path=db)
    assert first["new"] == 2 and first["row_count"] == 2 and not first["duplicate"] and len(first["sha256"]) == 64
    assert ingest_overture(hist, db_path=db)["duplicate"]
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
    assert con.execute("SELECT source_id, row_count FROM batches").fetchone() == ("overture-history-db", 2)
    con.close()
    assert len(api.search("Casa Grande", "restaurant", db_path=db)) == 2
    _history(tmp_path / "h2.db", 3)
    assert not ingest_overture(tmp_path / "h2.db", db_path=db)["duplicate"]


def test_overture_cli(tmp_path, monkeypatch, capsys):
    _history(tmp_path / "history.db")
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "d.db"))
    monkeypatch.delenv("STEPHANIE_DB", raising=False)
    assert main(["ingest-overture", "--history-db", str(tmp_path / "history.db")]) == 0
    assert "new: 2" in capsys.readouterr().out
    assert main(["ingest-overture", "--history-db", str(tmp_path / "nope.db")]) == 1
    assert main(["ingest-overture"]) == 1
    assert "--history-db" in capsys.readouterr().err
    monkeypatch.setenv("STEPHANIE_DB", str(tmp_path / "history.db"))
    assert main(["ingest-overture"]) == 0
