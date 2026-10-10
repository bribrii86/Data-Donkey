"""v0.5.4: Overture release history and SBA loans. Synthetic rows, fake fetcher; no network."""
import csv
from datetime import date

import pytest

import json

from donkey import api, overture, triggers
from donkey.ingest import connect, ingest, main

LISTING = ("<ListBucketResult><CommonPrefixes><Prefix>release/2026-08-19.0/</Prefix></CommonPrefixes>"
           "<CommonPrefixes><Prefix>release/2026-09-23.0/</Prefix></CommonPrefixes>"
           "<CommonPrefixes><Prefix>release/2026-09-23.1/</Prefix></CommonPrefixes>"
           "<CommonPrefixes><Prefix>release/junk/</Prefix></CommonPrefixes></ListBucketResult>")
AUGUST = [
    {"name": "Desert Rose Plumbing", "street": "123 W Sunny Ln", "city": "Maricopa", "zip": "85138",
     "phone": "+15205550101", "website": "", "category": "plumber"},
    {"name": "Phoenix Only Signs", "street": "1 N Central Ave Ste 1", "city": "Phoenix", "zip": "85004",
     "phone": "+16025550100", "website": "", "category": "sign_shop"},
]
SEPTEMBER = [
    {**AUGUST[0], "street": "450 N John Wayne Pkwy Ste 110", "website": "https://desertrose.test"},
    AUGUST[1],
    {"name": "Copper Sky Dental", "street": "9 W Main St Ste 2", "city": "Casa Grande", "zip": "85122",
     "phone": "+15205550199", "website": "", "category": "dentist"},
]


def fake_fetch(release, area):
    assert area["bbox"] == [-112.42, -111.26, 32.58, 33.19]
    return {"2026-08-19.0": AUGUST, "2026-09-23.1": SEPTEMBER}[release]


def test_releases_keeps_latest_per_date():
    assert overture.releases(LISTING) == ["2026-08-19.0", "2026-09-23.1"]


def test_overture_history_backfills_then_only_new(tmp_path):
    db = tmp_path / "donkey.db"
    saved = date(2026, 10, 10)
    done = overture.snapshot_new("stephanie", fake_fetch, LISTING, db_path=db, out_dir=tmp_path, today=saved)
    assert [(d["release"], d["rows"]) for d in done] == [("2026-08-19.0", 1), ("2026-09-23.1", 2)]  # Phoenix dropped
    assert done[0]["diff"]["baseline"] is True
    assert done[1]["diff"]["by_kind"] == {"growth_home_to_office": 1, "growth_new_business": 1,
                                          "growth_new_website": 1}
    with (tmp_path / "overture-2026-09-23.1-stephanie.csv").open(encoding="utf8") as stream:
        assert {r["date_seen"] for r in csv.DictReader(stream)} == {"2026-09-23"}
    assert overture.snapshot_new("stephanie", fake_fetch, LISTING, db_path=db, out_dir=tmp_path) == []
    # Dated the day it was saved, so the Aug-to-Sep changes are on this week's list. Overture scores are lower:
    # home-to-office 3 + website 1; a bare Overture "new business" (1 point) stays off the list.
    events = api.events("growth_new_business", db_path=db)
    assert [(e["event_date"], json.loads(e["details_json"])["score"]) for e in events] == [("2026-10-10", 1)]
    hits = api.call_this_week("stephanie", as_of="2026-10-12", db_path=db)
    assert [(h["name"], h["score"], h["signals"]) for h in hits] == [
        ("Desert Rose Plumbing", 4, ["home_to_office", "new_website"])]
    assert api.call_this_week("stephanie", as_of="2026-10-12", min_score=1, db_path=db)[1]["name"] == "Copper Sky Dental"


def test_area_without_bbox_is_refused(tmp_path, monkeypatch):
    custom = tmp_path / "areas.json"
    custom.write_text('{"dealer2": {"zips": ["85701"]}}', encoding="utf8")
    monkeypatch.setenv("DONKEY_AREAS", str(custom))
    try:
        overture.snapshot_release("2026-09-23.0", "dealer2", fake_fetch, db_path=tmp_path / "d.db", out_dir=tmp_path)
    except ValueError as exc:
        assert "no bbox" in str(exc)
    else:
        raise AssertionError("expected ValueError")


SBA_HEADER = ["AsOfDate", "Program", "BorrName", "BorrStreet", "BorrCity", "BorrState", "BorrZip", "BankName",
              "GrossApproval", "ApprovalDate", "NaicsCode", "NaicsDescription", "BusinessAge", "LoanStatus",
              "JobsSupported"]
SBA_ROWS = [
    ["20260630", "7A", "Synthetic Vet Clinic LLC", "10 E Florence Blvd", "Casa Grande", "AZ", "85122",
     "Example Bank", "350000", "05/14/2026", "541940", "Veterinary Services", "Existing or more than 2 years old",
     "EXEMPT", "12"],
    ["20260630", "7A", "Old Loan Bakery", "2 Main St", "Eloy", "AZ", "85131", "Example Bank", "50000", "03/02/2015",
     "311811", "Retail Bakeries", "New Business or 2 years or less", "PIF", "3"],
    ["20260630", "504", "Elsewhere Widgets Inc", "5 Oak St", "Denver", "CO", "80202", "Example CDC", "900000",
     "06/01/2026", "332999", "Misc Fabricated Metal", "Existing or more than 2 years old", "EXEMPT", "40"],
]


def test_sba_loans_keep_arizona_and_score(tmp_path, capsys, monkeypatch):
    path = tmp_path / "foia-7a-fy2010-present.csv"
    with path.open("w", encoding="utf8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(SBA_HEADER)
        writer.writerows(SBA_ROWS)
    db = tmp_path / "donkey.db"
    result = ingest("sba-loans", path, "data.sba.gov", db)
    assert result["row_count"] == 3 and result["skipped"] == 1 and result["new"] == 2
    loans = {e["business_name"]: e for e in api.events("sba_loan", db_path=db)}
    assert set(loans) == {"Synthetic Vet Clinic LLC", "Old Loan Bakery"} and loans["Old Loan Bakery"]["event_date"] == "2015-03-02"
    hits = api.call_this_week("stephanie", as_of="2026-10-01", db_path=db)
    assert [(h["name"], h["signals"], h["reasons"]) for h in hits] == [
        ("Synthetic Vet Clinic LLC", ["sba_loan"], ["SBA loan approved"])]
    assert hits[0]["details"][0]["amount"] == "350000"
    assert triggers.SBA_LOOKBACK_DAYS == 180

    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "cli.db"))
    assert main(["ingest", "--source", "sba-loans", "--file", str(path), "--received-from", "data.sba.gov"]) == 0
    assert "left out by the map's filter: 1" in capsys.readouterr().out


def test_overture_cli_reports(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    monkeypatch.setattr(overture, "releases", lambda listing=None: ["2026-08-19.0"])
    monkeypatch.setattr(overture, "fetch_rows", fake_fetch)
    monkeypatch.setattr(overture, "DEFAULT_DB", tmp_path / "donkey.db")
    assert main(["overture-snapshot"]) == 0
    assert "Overture 2026-08-19.0 (stephanie): 1 places; first release stored" in capsys.readouterr().out
    assert main(["overture-snapshot"]) == 0
    assert "No new Overture release" in capsys.readouterr().out


def _sba_file(path, header, rows):
    with path.open("w", encoding="utf8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def test_sba_wrong_columns_refused_and_not_burned(tmp_path):
    db = tmp_path / "donkey.db"
    renamed = [h.replace("BorrState", "Borrower_St") for h in SBA_HEADER]  # a future file renames a column
    bad = _sba_file(tmp_path / "foia.csv", renamed, SBA_ROWS)
    with pytest.raises(ValueError, match="no column found for state"):
        ingest("sba-loans", bad, "data.sba.gov", db)
    no_arizona = _sba_file(tmp_path / "co.csv", SBA_HEADER, [SBA_ROWS[2]])
    with pytest.raises(ValueError, match="kept 0 of 1 rows"):
        ingest("sba-loans", no_arizona, "data.sba.gov", db)
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 0  # neither file is marked as ingested
    con.close()
    good = _sba_file(tmp_path / "foia.csv", SBA_HEADER, SBA_ROWS)  # same name, fixed header: goes in
    assert ingest("sba-loans", good, "data.sba.gov", db)["new"] == 2
