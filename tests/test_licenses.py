"""v0.6.5: city business licenses (source map city-licenses) score on call-this-week. Made-up businesses."""
import csv
from datetime import date

from donkey import api
from donkey.ingest import ingest

TODAY = date(2026, 10, 19)
COLUMNS = ["Business Name", "Legal Name", "Address", "City", "State", "ZIP", "License Number", "Issue Date",
           "Classification"]


def _licenses(path, rows):
    with path.open("w", encoding="utf8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(COLUMNS)
        writer.writerows(rows)
    return path


def test_recent_licenses_score_and_renewals_and_old_ones_do_not(tmp_path):
    db = tmp_path / "donkey.db"
    rows = [
        ["Sample Test Dental", "Sample Test Dental PLLC", "10 W Test St Ste 2", "Casa Grande", "AZ", "85122",
         "BL-26-00001", "2026-09-30", "Service"],
        ["Example Test Realty", "Example Test Realty LLC", "20 E Test Rd", "Casa Grande", "AZ", "85122",
         "BL-26-00002", "2026-01-01", "Service"],  # Jan 1: an annual renewal
        ["Old Test Bakery", "Old Test Bakery LLC", "30 N Test Ave", "Casa Grande", "AZ", "85122",
         "BL-26-00003", "2026-06-15", "Retailer"],  # more than 60 days ago
        ["Faraway Test Shop", "Faraway Test Shop LLC", "1 Test Way", "Tucson", "AZ", "85701",
         "BL-26-00004", "10-01-2026", "Retailer"],  # outside her area; city export date format
    ]
    result = ingest("city-licenses", _licenses(tmp_path / "lic.csv", rows), "test", db)
    assert result["new"] == 4 and result["unmapped"] == ["naics", "vertical", "contact_name", "contact_title",
                                                         "contact_email", "contact_phone", "phone", "website"]
    hits = api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=db)
    assert [(h["name"], h["score"], h["reasons"]) for h in hits] == [
        ("Sample Test Dental", 3 + 1, ["new city business license", hits[0]["reasons"][1]])]  # +1 print-heavy (dental)
    assert api.lookup("Faraway Test Shop", db_path=db)[0]["open_date"] == "2026-10-01"  # MM-DD-YYYY parsed


def test_license_list_without_issue_dates_is_refused(tmp_path):
    path = tmp_path / "lic.csv"
    with path.open("w", encoding="utf8", newline="") as stream:
        csv.writer(stream).writerows([["Business Name", "City"], ["Sample Test Dental", "Casa Grande"]])
    try:
        ingest("city-licenses", path, "test", tmp_path / "donkey.db")
    except ValueError as exc:
        assert "event_date" in str(exc)
    else:
        raise AssertionError("a license file with no issue date column must be refused")
