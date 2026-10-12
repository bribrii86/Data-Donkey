import csv
import json
import sqlite3

import pytest

from donkey import __version__, api
from donkey.ingest import connect, ingest, main


def _city_file(path, rows):
    columns = ["Business Name", "Address", "City", "ZIP", "License No", "Open Date", "NAICS", "Vertical"]
    with path.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_city_ingest_keeps_raw_records_and_duplicate_is_noop(tmp_path):
    source_file = _city_file(
        tmp_path / "city.csv",
        [{"Business Name": "Copper Mesa Dental", "Address": "101 S Desert Ave", "City": "Casa Grande",
          "ZIP": "85122", "License No": "SYN-1", "Open Date": "03/12/2024", "NAICS": "621210",
          "Vertical": "dental"}],
    )
    db = tmp_path / "donkey.db"
    first = ingest("city-business-list", source_file, "Synthetic city sample", db)
    second = ingest("city-business-list", source_file, "Other sender", db)

    assert first["new"] == 1 and first["row_count"] == 1 and not first["duplicate"]
    assert second["duplicate"] and second["row_count"] == 0
    con = connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM batches").fetchone()[0] == 1
        record = con.execute("SELECT raw_json, match_status FROM records").fetchone()
        assert json.loads(record["raw_json"])["License No"] == "SYN-1"
        assert json.loads(record["raw_json"])["Open Date"] == "03/12/2024"
        assert record["match_status"] == "new"
    finally:
        con.close()
    assert api.search("casa grande", "dental", db_path=db)[0]["name"] == "Copper Mesa Dental"
    assert api.lookup("Copper Mesa Dental", db_path=db)[0]["zip"] == "85122"
    assert api.lookup("Copper Mesa Dental", db_path=db)[0]["open_date"] == "2024-03-12"


def test_exact_zip_match_and_ambiguous_rows_are_never_merged(tmp_path):
    source = _city_file(
        tmp_path / "first.csv",
        [{"Business Name": "Sunrise Accounting", "Address": "42 N Pinal St", "City": "Casa Grande", "ZIP": "85122"}],
    )
    db = tmp_path / "donkey.db"
    assert ingest("city-business-list", source, "Synthetic", db)["new"] == 1

    match_source = _city_file(
        tmp_path / "match.csv",
        [{"Business Name": "Sunrise Accounting LLC", "Address": "different street", "City": "Casa Grande",
          "ZIP": "85122"}],
    )
    assert ingest("city-business-list", match_source, "Synthetic", db)["matched"] == 1

    con = connect(db)
    try:
        original = con.execute("SELECT id, normalized_name, zip, street FROM businesses").fetchone()
        for _ in range(2):
            con.execute(
                """INSERT INTO businesses
                (name, normalized_name, zip, street, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'now', 'now')""",
                ("Sunrise Accounting", original["normalized_name"], original["zip"], original["street"]),
            )
        con.commit()
    finally:
        con.close()

    ambiguous_source = _city_file(
        tmp_path / "ambiguous.csv",
        [{"Business Name": "Sunrise Accounting", "Address": "42 North Pinal Street", "City": "Casa Grande",
          "ZIP": "85122"}],
    )
    result = ingest("city-business-list", ambiguous_source, "Synthetic", db)
    assert result["needs_review"] == 1
    review_rows = api.needs_review(db_path=db)
    assert len(review_rows) == 1
    assert len(json.loads(review_rows[0]["review_candidates_json"])) == 3


def test_ucc_events_and_read_only_queries(tmp_path, capsys, monkeypatch):
    ucc = tmp_path / "ucc.csv"
    ucc.write_text("Debtor,Filing Date,File Number,Secured Party\nSynthetic Dental,2026-01-02,F-1,Synthetic Bank\n",
                   encoding="utf8")
    db = tmp_path / "donkey.db"
    monkeypatch.setenv("DONKEY_DB", str(db))
    assert main(["ingest", "--source", "az-sos-ucc", "--file", str(ucc), "--received-from", "Synthetic source"]) == 0
    assert "new: 1; matched: 0; needs-review: 0" in capsys.readouterr().out
    listed = api.events("ucc_filing", since="2026-01-01", db_path=db)
    assert listed[0]["business_name"] == "Synthetic Dental"
    assert json.loads(listed[0]["details_json"]) == {
        "name": "Synthetic Dental", "filing_number": "F-1", "secured_party": "Synthetic Bank", "zip": "",
        "lessor_type": "bank", "lease_window_start": "", "lease_end_est": "",
    }  # v0.5.2 added name, zip and the UCC tags; the original keys are unchanged
    con = api._read_connection(db)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM events")
    finally:
        con.close()


def test_ucc_mdy_dates_filter_by_iso_date_and_keep_raw_values(tmp_path):
    ucc = tmp_path / "dates.csv"
    ucc.write_text(
        "Debtor,Filing Date,File Number,Secured Party\n"
        "Synthetic Early Filing,12/31/2023,BEFORE,Synthetic Bank\n"
        "Synthetic Recent Filing,01/15/2024,AFTER,Synthetic Bank\n",
        encoding="utf8",
    )
    db = tmp_path / "donkey.db"
    assert ingest("az-sos-ucc", ucc, "Synthetic fixture", db)["new"] == 2

    events = api.events("ucc_filing", since="2024-01-01", db_path=db)
    assert [event["business_name"] for event in events] == ["Synthetic Recent Filing"]
    assert events[0]["event_date"] == "2024-01-15"
    con = connect(db)
    try:
        records = con.execute("SELECT raw_json FROM records ORDER BY row_number").fetchall()
        assert json.loads(records[0]["raw_json"])["Filing Date"] == "12/31/2023"
        assert json.loads(records[1]["raw_json"])["Filing Date"] == "01/15/2024"
    finally:
        con.close()


def test_repeated_name_only_ucc_rows_share_unique_business(tmp_path):
    ucc = tmp_path / "repeat-debtor.csv"
    ucc.write_text(
        "Debtor,Filing Date,File Number,Secured Party\n"
        "Synthetic Repeat Debtor,02/01/2024,FIRST,Synthetic Bank\n"
        "Synthetic Repeat Debtor,03/01/2024,SECOND,Synthetic Bank\n",
        encoding="utf8",
    )
    db = tmp_path / "donkey.db"

    result = ingest("az-sos-ucc", ucc, "Synthetic fixture", db)
    filings = api.events("ucc_filing", db_path=db)
    con = connect(db)
    try:
        business_count = con.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
    finally:
        con.close()

    assert result["new"] == 1 and result["matched"] == 1 and result["needs_review"] == 0
    assert business_count == 1
    assert len(filings) == 2
    assert len(api.needs_review(db_path=db)) == 0


def test_overture_jsonl_nested_columns(tmp_path):
    source = tmp_path / "places.jsonl"
    source.write_text(
        '{"properties":{"name":"Synthetic Accounting","address":{"freeform":"123 Main St",'
        '"locality":"Mesa","region":"AZ","postcode":"85201"},"primary_category":"accountant"}}\n',
        encoding="utf8",
    )
    db = tmp_path / "donkey.db"
    result = ingest("overture-places", source, "Synthetic fixture", db)
    assert result["new"] == 1
    business = api.search("Mesa", "accountant", db_path=db)[0]
    assert (business["name"], business["state"], business["zip"]) == ("Synthetic Accounting", "AZ", "85201")


def test_donkey_version_and_custom_db_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "custom.db"))
    assert __version__ == "0.5.7"
    con = connect()
    try:
        assert con.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    finally:
        con.close()
