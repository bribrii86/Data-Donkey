"""v0.5.1: growth snapshots, diff and score. Synthetic rows only."""
import csv

from donkey import api, growth
from donkey.ingest import main

COLUMNS = ["name", "street", "city", "zip", "phone", "website", "category", "date_seen"]

WEEK1 = [
    {"name": "Desert Rose Plumbing", "street": "123 W Sunny Ln", "city": "Maricopa", "zip": "85138",
     "phone": "(520) 555-0101", "website": "", "category": "Plumber", "date_seen": "2026-10-05"},
    {"name": "Copper Mesa Dental", "street": "101 S Desert Ave Ste 4", "city": "Casa Grande", "zip": "85122",
     "phone": "520-555-0102", "website": "", "category": "dentist", "date_seen": "2026-10-05"},
    {"name": "Saguaro Tutoring", "street": "9 E Palo Verde Dr", "city": "Casa Grande", "zip": "85122",
     "phone": "", "website": "", "category": "tutor", "date_seen": "2026-10-05"},
    {"name": "Gone Away Insurance", "street": "5 N Main St Suite 2", "city": "Eloy", "zip": "85131",
     "phone": "520-555-0104", "website": "", "category": "insurance", "date_seen": "2026-10-05"},
]
WEEK2 = [
    # graduated: home street to a suite, same phone, new ZIP
    {**WEEK1[0], "street": "450 N Pinal Ave Suite 110", "zip": "85122", "city": "Casa Grande",
     "website": "https://desertroseplumbing.example", "date_seen": "2026-10-12"},
    {**WEEK1[1], "street": "2000 N Trekell Rd Ste 9", "date_seen": "2026-10-12"},  # moved office to office
    {**WEEK1[2], "phone": "520-555-0103", "date_seen": "2026-10-12"},  # got a business line
    {"name": "Maricopa Family Chiropractic", "street": "20800 N John Wayne Pkwy #105", "city": "Maricopa",
     "zip": "85139", "phone": "520-555-0105", "website": "", "category": "chiropractor", "date_seen": "2026-10-12"},
    # Gone Away Insurance is missing: not reported (likely a missed page, not a closure)
]


def _csv(path, rows):
    with path.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_home_based_flag():
    assert growth.is_home_based(WEEK1[0])  # plumber at a house number, no suite
    assert not growth.is_home_based(WEEK1[1])  # suite
    assert not growth.is_home_based({"street": "1 Main St", "category": "church"})
    assert growth.is_home_based({"street": "PO Box 12", "category": "church"})
    assert not growth.is_home_based({"street": "20987 N John Wayne Pkwy", "category": "Painting Contractors"})
    assert not growth.is_home_based({**WEEK1[0], "home_based": "no"})  # an explicit column wins


def test_key_keeps_clinic_doctors_apart_and_survives_a_move():
    a = {"name": "Abad Maria M MD", "phone": "520-316-0688", "zip": "85122"}
    b = {"name": "Lee Jon MD", "phone": "520-316-0688", "zip": "85122"}
    assert growth.row_key(a) != growth.row_key(b)
    assert growth.row_key(a) == growth.row_key({**a, "zip": "85193", "phone": "1 (520) 316-0688"})


def test_diff_rows_finds_each_growth_signal():
    kinds = {(c["after"]["name"], c["kind"]) for c in growth.diff_rows(WEEK1, WEEK2)}
    assert kinds == {
        ("Desert Rose Plumbing", "growth_home_to_office"),
        ("Desert Rose Plumbing", "growth_new_website"),
        ("Copper Mesa Dental", "growth_moved"),
        ("Saguaro Tutoring", "growth_new_phone"),  # matched on name + ZIP when the phone first appears
        ("Maricopa Family Chiropractic", "growth_new_business"),
    }
    assert growth.diff_rows(WEEK1, WEEK1) == []
    listed_twice = WEEK1 + [{**WEEK1[0], "street": "1 Other St"}]  # one business, two addresses in one file
    assert growth.diff_rows(listed_twice, listed_twice) == []


def test_snapshot_diff_events_and_score(tmp_path):
    db = tmp_path / "donkey.db"
    first = growth.take_snapshot(_csv(tmp_path / "w1.csv", WEEK1), "territory directory", db_path=db)
    assert first["row_count"] == 4 and first["home_based"] == 2
    assert growth.diff_snapshots(db_path=db)["baseline"]
    assert growth.take_snapshot(tmp_path / "w1.csv", "territory directory", db_path=db)["duplicate"]

    growth.take_snapshot(_csv(tmp_path / "w2.csv", WEEK2), "territory directory", db_path=db)
    result = growth.diff_snapshots(db_path=db)
    assert result["changes"] == 5 and result["by_kind"]["growth_new_business"] == 1
    assert growth.diff_snapshots(db_path=db)["duplicate"]  # same pair twice adds nothing

    graduated = api.events("growth_home_to_office", ["Casa Grande"], since="2026-10-12", db_path=db)
    assert [e["details_json"] for e in graduated][0].count("Desert Rose Plumbing") == 1

    hits = api.growth(["Casa Grande", "Maricopa"], since="2026-10-06", db_path=db)
    assert [(h["name"], h["score"]) for h in hits] == [
        ("Desert Rose Plumbing", 6), ("Copper Mesa Dental", 3), ("Maricopa Family Chiropractic", 3),
    ]  # Saguaro Tutoring's new phone alone (1 point) stays below the bar
    assert hits[0]["signals"] == ["home_to_office", "new_website"]
    assert api.growth(["Eloy"], db_path=db) == []


def test_growth_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    assert main(["growth-snapshot", "--file", str(_csv(tmp_path / "w1.csv", WEEK1)), "--label", "t"]) == 0
    assert main(["growth-diff"]) == 0
    assert main(["growth-snapshot", "--file", str(_csv(tmp_path / "w2.csv", WEEK2)), "--label", "t"]) == 0
    assert main(["growth-diff"]) == 0
    out = capsys.readouterr().out
    assert "nothing to compare yet" in out and "5 changes" in out and "home_to_office: 1" in out
    assert main(["growth-snapshot", "--file", str(tmp_path / "missing.csv"), "--label", "t"]) == 1
