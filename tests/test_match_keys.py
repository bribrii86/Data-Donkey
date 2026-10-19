"""v0.6.4: phone and website-domain match keys. All names, phones and domains are made up."""
import csv
import sqlite3

import pytest

from donkey.ingest import connect, domain_key, ingest, phone_key

COLUMNS = ["Business Name", "Address", "City", "ZIP", "Phone", "Website"]


def _file(path, rows):
    with path.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def _row(name, street, zip_code, phone="", website=""):
    return {"Business Name": name, "Address": street, "City": "Casa Grande", "ZIP": zip_code, "Phone": phone,
            "Website": website}


@pytest.mark.parametrize("value, key", [
    ("(520) 555-0142", "5205550142"), ("+1 520.555.0142", "5205550142"), ("555-0142", ""),
    ("1-800-555-0199", ""), ("(888) 555-0100", ""), ("", ""),
])
def test_phone_key(value, key):
    assert phone_key(value) == key


@pytest.mark.parametrize("value, key", [
    ("https://www.Example-Dental.com/about", "example-dental.com"), ("example-dental.com", "example-dental.com"),
    ("http://shop.example-dental.com:8080", "shop.example-dental.com"), ("https://facebook.com/sampledental", ""),
    ("sampledental.wixsite.com/home", ""), ("not a site", ""), ("", ""),
])
def test_domain_key(value, key):
    assert domain_key(value) == key


def test_same_name_and_phone_at_a_new_address_is_the_same_business(tmp_path):
    db = tmp_path / "donkey.db"
    ingest("city-business-list", _file(tmp_path / "a.csv", [_row("Sample Mesa Dental", "10 W Test St", "85122",
                                                                   "(520) 555-0142")]), "test", db)
    moved = ingest("city-business-list", _file(tmp_path / "b.csv", [_row("Sample Mesa Dental", "900 E Example Rd",
                                                                         "85193", "520-555-0142")]), "test", db)
    assert moved["matched"] == 1 and moved["new"] == 0


def test_chain_stores_sharing_a_domain_stay_separate(tmp_path):
    db = tmp_path / "donkey.db"
    rows = [_row("Example Quick Mart", "1 N Test Ave", "85122", "(520) 555-0101", "https://examplequickmart.com"),
            _row("Example Quick Mart", "2 S Test Ave", "85193", "(520) 555-0102", "https://examplequickmart.com")]
    assert ingest("city-business-list", _file(tmp_path / "a.csv", rows), "test", db)["new"] == 2


def test_doctors_sharing_a_clinic_phone_stay_separate(tmp_path):
    db = tmp_path / "donkey.db"
    rows = [_row("Dr Sample One", "5 Test Pkwy", "85122", "(520) 555-0150"),
            _row("Dr Sample Two", "5 Test Pkwy", "85122", "(520) 555-0150")]
    assert ingest("city-business-list", _file(tmp_path / "a.csv", rows), "test", db)["new"] == 2


def test_different_spelling_with_same_phone_and_domain_matches(tmp_path):
    db = tmp_path / "donkey.db"
    ingest("city-business-list", _file(tmp_path / "a.csv", [_row("Sample Title And Escrow", "7 Test Blvd", "85122",
                                                                   "(520) 555-0177", "sampletitle.com")]), "test", db)
    again = ingest("city-business-list", _file(tmp_path / "b.csv", [_row("Sample Title Agency", "7 Test Blvd Ste 2",
                                                                         "85122", "520 555 0177",
                                                                         "https://www.sampletitle.com")]), "test", db)
    assert again["matched"] == 1


def test_same_name_tie_in_town_is_broken_by_domain(tmp_path):
    db = tmp_path / "donkey.db"
    rows = [_row("Sample Builders", "1 Test Rd", "85122", website="samplebuilders-cg.com"),
            _row("Sample Builders", "2 Test Rd", "85193", website="samplebuilders-az.com")]
    ingest("city-business-list", _file(tmp_path / "a.csv", rows), "test", db)
    crust = tmp_path / "crust.csv"
    with crust.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["basic_info.name", "basic_info.primary_domain",
                                                    "locations.headquarters"])
        writer.writeheader()
        writer.writerow({"basic_info.name": "Sample Builders", "basic_info.primary_domain": "samplebuilders-az.com",
                         "locations.headquarters": "Casa Grande, Arizona, United States"})
    result = ingest("crustdata-companies", crust, "test", db)
    assert result["matched"] == 1 and result["needs_review"] == 0


def test_older_database_gets_keys_from_stored_rows(tmp_path):
    db = tmp_path / "donkey.db"
    ingest("city-business-list", _file(tmp_path / "a.csv", [_row("Sample Mesa Dental", "10 W Test St", "85122",
                                                                   "(520) 555-0142", "samplemesadental.com")]),
           "test", db)
    raw = sqlite3.connect(db)  # make it look like a v0.6.3 database: no key columns yet
    raw.execute("DROP INDEX idx_businesses_phone")
    raw.execute("DROP INDEX idx_businesses_domain")
    raw.execute("ALTER TABLE businesses DROP COLUMN phone_key")
    raw.execute("ALTER TABLE businesses DROP COLUMN domain_key")
    raw.commit()
    raw.close()
    con = connect(db)
    try:
        row = con.execute("SELECT phone_key, domain_key FROM businesses").fetchone()
        assert (row["phone_key"], row["domain_key"]) == ("5205550142", "samplemesadental.com")
    finally:
        con.close()
