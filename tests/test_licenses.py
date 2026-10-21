"""v0.6.5: city business licenses (source map city-licenses) score on call-this-week. Made-up businesses."""
import csv
from datetime import date

import pytest

from donkey import api
from donkey.triggers import looks_home_based
from donkey.ingest import ingest, main

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
    assert [(h["name"], h["score"], h["signals"]) for h in hits] == [("Sample Test Dental", 3 + 1, ["new_license", "print_heavy"])]
    assert hits[0]["reasons"][0] == "new city business license"
    assert api.lookup("Faraway Test Shop", db_path=db)[0]["open_date"] == "2026-10-01"  # MM-DD-YYYY parsed


def test_license_list_without_issue_dates_is_refused(tmp_path):
    path = tmp_path / "lic.csv"
    with path.open("w", encoding="utf8", newline="") as stream:
        csv.writer(stream).writerows([["Business Name", "City"], ["Sample Test Dental", "Casa Grande"]])
    with pytest.raises(ValueError, match="event_date"):
        ingest("city-licenses", path, "test", tmp_path / "donkey.db")


def test_window_edges_kiosks_and_one_signal_per_business(tmp_path):
    db = tmp_path / "donkey.db"
    rows = [
        ["Edge Test Office", "", "1 Test St", "Casa Grande", "AZ", "85122", "BL-1", "2026-08-20", "Service"],  # day 60
        ["Late Test Office", "", "2 Test St", "Casa Grande", "AZ", "85122", "BL-2", "2026-08-19", "Service"],  # day 61
        ["Future Test Office", "", "3 Test St", "Casa Grande", "AZ", "85122", "BL-3", "2026-11-01", "Service"],
        ["Coinme", "", "4 Test St", "Casa Grande", "AZ", "85122", "BL-4", "2026-10-01", "Retailer"],
        ["ECOATM LLC", "", "4 Test St Ste 1", "Casa Grande", "AZ", "85122", "BL-9", "2026-10-01", "Wholesale"],
        ["Twice Test Cafe", "", "5 Test St", "Casa Grande", "AZ", "85122", "BL-5", "2026-10-01", "Restaurant"],
        ["Twice Test Cafe", "", "5 Test St", "Casa Grande", "AZ", "85122", "BL-6", "2026-10-02", "Retailer"],
        ["Blank Test Date", "", "6 Test St", "Casa Grande", "AZ", "85122", "BL-7", "", "Service"],
        ["Town Only Test Shop", "", "7 Test St", "Coolidge", "AZ", "", "BL-8", "2026-10-03", "Service"],
        ["Sample Test Cleaning LLC", "Pat Example", "8 N Test Rd Apt 12", "Casa Grande", "AZ", "85122", "BL-10",
         "2026-10-03", "Service"],  # a person's own license at an apartment: home business
    ]
    ingest("city-licenses", _licenses(tmp_path / "lic.csv", rows), "test", db)
    hits = {h["name"]: h for h in api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=db)}
    assert set(hits) == {"Edge Test Office", "Twice Test Cafe", "Town Only Test Shop"}
    assert hits["Twice Test Cafe"]["signals"] == ["new_license"] and hits["Twice Test Cafe"]["score"] == 3


def test_same_file_under_city_business_list_first_is_named(tmp_path, monkeypatch, capsys):
    path = _licenses(tmp_path / "lic.csv", [["Sample Test Dental", "", "1 Test St", "Casa Grande", "AZ", "85122",
                                             "BL-1", "2026-10-01", "Service"]])
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    assert main(["ingest", "--source", "city-business-list", "--file", str(path), "--received-from", "t"]) == 0
    capsys.readouterr()
    assert main(["ingest", "--source", "city-licenses", "--file", str(path), "--received-from", "t"]) == 0
    assert "Already ingested with --source city-business-list" in capsys.readouterr().out


@pytest.mark.parametrize("legal, street, dba, home", [
    ("Pat Example", "8 N Test Rd Apt 12", "Sample Cleaning", True), ("Pat Q. Example", "12 E Sample Ln", "", True),
    ("Pat Example", "9 E Test Blvd Ste 3", "Sample Cleaning", False),
    ("Pat Example", "9 E Test Blvd Unit B", "Sample Cleaning", False),  # a shop suite, not a home
    ("Sample Test Cleaning LLC", "12 E Sample Ln", "", False), ("Grace Test Church", "1 E Test Ln", "", False),
    ("Desert Test Tire", "40 W Test Dr", "Desert Test Tire", False),  # the trade name itself, not a person
    ("", "12 E Sample Ln", "", False),
])
def test_home_based(legal, street, dba, home):
    assert looks_home_based(legal, street, dba) is home


def test_rows_carry_address_phone_and_website(tmp_path):
    """v0.6.6: the Monday list needs somewhere to call: the matched business's phone and site, else the license street."""
    db = tmp_path / "donkey.db"
    known = tmp_path / "list.csv"
    with known.open("w", encoding="utf8", newline="") as stream:
        csv.writer(stream).writerows([["Business Name", "Address", "City", "State", "ZIP", "Business Phone", "Website"],
                                      ["Sample Test Salon", "40 W Test St", "Casa Grande", "AZ", "85122",
                                       "520-555-0142", "https://www.sampletestsalon.example/book"]])
    ingest("city-business-list", known, "test", db)
    rows = [["Sample Test Salon", "", "40 W Test St", "Casa Grande", "AZ", "85122", "BL-1", "2026-10-01", "Service"],
            ["New Test Cafe", "", "50 E Test Rd", "Casa Grande", "AZ", "85122", "BL-2", "2026-10-01", "Retailer"]]
    ingest("city-licenses", _licenses(tmp_path / "lic.csv", rows), "test", db)
    hits = {h["name"]: h for h in api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=db)}
    salon, cafe = hits["Sample Test Salon"], hits["New Test Cafe"]
    assert (salon["phone"], salon["website"]) == ("(520) 555-0142", "https://sampletestsalon.example")
    assert salon["address"].startswith("40 W Test St") and "Casa Grande" in salon["address"]
    assert cafe["address"].startswith("50 E Test Rd") and "85122" in cafe["address"]
    assert (cafe["phone"], cafe["website"]) == ("", "")


def _item(name, zip_code, town, *details):
    return {"name": name, "zip": zip_code, "town": town, "signals": [d["signal"] for d in details],
            "details": list(details)}


def test_contact_edge_cases(tmp_path):
    import sqlite3

    from donkey.triggers import _contacts

    db = tmp_path / "donkey.db"
    known = tmp_path / "list.csv"
    with known.open("w", encoding="utf8", newline="") as stream:
        csv.writer(stream).writerows([
            ["Business Name", "Address", "City", "State", "ZIP", "Business Phone"],
            ["Twin Test Cafe", "1 N Test St", "Casa Grande", "AZ", "85122", "520-555-0101"],
            ["Zip Test Shop", "5 Test Ave, Casa Grande, AZ 85122", "", "AZ", "85122", "520-555-0103"],
            ["Agent Test LLC", "7 Lawyer Way", "Casa Grande", "AZ", "85122", "520-555-0104"]])
    ingest("city-business-list", known, "test", db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    con.execute("""INSERT INTO businesses (name, normalized_name, address, street, city, state, zip, created_at,
        updated_at, phone_key) VALUES ('Twin Test Cafe', 'twin test cafe', '900 E Other Rd', '900 E Other Rd',
        'Casa Grande', 'AZ', '85122', 'x', 'x', '5205550102')""")  # a needs-review twin: two places, one name
    lic = {"signal": "new_license", "street": "900 E Other Rd"}
    items = [_item("Twin Test Cafe", "85122", "casa grande", {"signal": "new_phone"}),  # two places: blank
             _item("Twin Test Cafe", "85122", "casa grande", lic),  # the license street picks one
             _item("Zip Test Shop", "85122", "casa grande", {"signal": "moved"}),
             _item("Twin Test Cafe", "", "", {"signal": "new_phone"}),  # no ZIP or town: no guess
             _item("Agent Test LLC", "85122", "casa grande", {"signal": "new_llc_agent"})]
    _contacts(con, items)
    assert (items[0]["address"], items[0]["phone"]) == ("", "")
    assert items[1]["address"] == "900 E Other Rd, Casa Grande, AZ 85122" and items[1]["phone"] == "(520) 555-0102"
    assert items[2]["address"] == "5 Test Ave, Casa Grande, AZ 85122"  # nothing appended twice
    assert (items[3]["address"], items[4]["address"], items[4]["phone"]) == ("", "", "")
    moved = _item("Twin Test Cafe", "85122", "casa grande", {**lic, "street": "77 New Test Blvd",
                                                              "business_id": 1})
    _contacts(con, [moved])  # a moved business: the license street, not the stored old one
    assert moved["address"].startswith("77 New Test Blvd") and moved["phone"] == "(520) 555-0101"
    for index in ("idx_businesses_phone", "idx_businesses_domain"):  # a pre-v0.6.4 database: addresses only
        con.execute(f"DROP INDEX IF EXISTS {index}")  # nosec B608 - fixed names
    con.execute("ALTER TABLE businesses DROP COLUMN phone_key")
    con.execute("ALTER TABLE businesses DROP COLUMN domain_key")
    old = _item("Zip Test Shop", "85122", "casa grande", {"signal": "moved"})
    _contacts(con, [old])
    assert old["address"].startswith("5 Test Ave") and old["phone"] == ""
    con.close()


def test_phone_from_weekly_listings(tmp_path):
    """v0.6.7: a license row with no phone takes it from the latest listings snapshot: like name, same ZIP and street."""
    from donkey import growth

    db = tmp_path / "donkey.db"
    snap = tmp_path / "snap.csv"
    cols = ["name", "street", "city", "zip", "phone", "website", "category", "date_seen"]
    with snap.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=cols)
        writer.writeheader()
        writer.writerows([
            {"name": "Sample Test Buses", "street": "26 North Test Road", "city": "Casa Grande", "zip": "85122",
             "phone": "+1 520-555-0150", "website": "https://buses.example", "category": "", "date_seen": "2026-10-01"},
            {"name": "Chain Test Subs", "street": "10 W Test Hwy", "city": "Casa Grande", "zip": "85122",
             "phone": "520-555-0160", "website": "", "category": "", "date_seen": "2026-10-01"},
            {"name": "Chain Test Subs", "street": "99 E Far Rd", "city": "Casa Grande", "zip": "85122",
             "phone": "520-555-0161", "website": "", "category": "", "date_seen": "2026-10-01"},
            {"name": "Other Test Tenant", "street": "50 E Mall Rd", "city": "Casa Grande", "zip": "85122",
             "phone": "520-555-0170", "website": "", "category": "", "date_seen": "2026-10-01"}])
    growth.take_snapshot(snap, "overture stephanie", db_path=db)
    rows = [["Sample Test Buses LP", "", "26 N Test Rd", "Casa Grande", "AZ", "85122", "BL-1", "2026-10-01", "Service"],
            ["Chain Test Subs", "", "99 E Far Rd", "Casa Grande", "AZ", "85122", "BL-2", "2026-10-01", "Retailer"],
            ["New Test Cafe", "", "50 E Mall Rd", "Casa Grande", "AZ", "85122", "BL-3", "2026-10-01", "Retailer"]]
    ingest("city-licenses", _licenses(tmp_path / "lic.csv", rows), "test", db)
    hits = {h["name"]: h for h in api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=db)}
    buses = hits["Sample Test Buses LP"]
    assert (buses["phone"], buses["website"]) == ("(520) 555-0150", "https://buses.example")
    assert buses["address"] == "26 N Test Rd, Casa Grande, AZ 85122"  # the license street stays
    assert hits["Chain Test Subs"]["phone"] == "(520) 555-0161"  # the store at the license's street, not the other
    assert hits["New Test Cafe"]["phone"] == ""  # same address, different business


def test_listing_without_street_needs_one_place(tmp_path):
    import sqlite3

    from donkey import growth
    from donkey.triggers import _contacts

    db = tmp_path / "donkey.db"
    snap = tmp_path / "snap.csv"
    cols = ["name", "street", "city", "zip", "phone", "website", "category", "date_seen"]
    with snap.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=cols)
        writer.writeheader()
        for street, phone in (("1 A St", "520-555-0181"), ("2 B St", "520-555-0182")):
            writer.writerow({"name": "Twin Test Clinic", "street": street, "city": "Casa Grande", "zip": "85122",
                             "phone": phone, "website": "", "category": "", "date_seen": "2026-10-01"})
        writer.writerow({"name": "Solo Test Shop", "street": "3 C St", "city": "Casa Grande", "zip": "85122",
                         "phone": "520-555-0183", "website": "", "category": "", "date_seen": "2026-10-01"})
    growth.take_snapshot(snap, "overture stephanie", db_path=db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    twin = {"name": "Twin Test Clinic", "zip": "85122", "town": "casa grande", "signals": ["new_phone"],
            "details": [{"signal": "new_phone"}]}
    solo = {**twin, "name": "Solo Test Shop", "details": [{"signal": "new_phone"}]}
    _contacts(con, [twin, solo])
    con.close()
    assert (twin["phone"], twin["address"]) == ("", "")
    assert (solo["phone"], solo["address"]) == ("(520) 555-0183", "3 C St, Casa Grande, AZ 85122")


def test_listings_never_borrow_a_neighbor(tmp_path):
    """Review fixes: two tenants of one building, a street with no house number, toll-free and social links, and a
    database whose listings can't be read."""
    import sqlite3

    from donkey import growth
    from donkey.triggers import _contacts

    db = tmp_path / "donkey.db"
    snap = tmp_path / "snap.csv"
    cols = ["name", "street", "city", "zip", "phone", "website", "category", "date_seen"]
    places = [("Grande Test Dental", "1004 N Mall Pkwy Ste 1", "520-555-0191", ""),
              ("Grande Test Vision", "1004 N Mall Pkwy Ste 2", "520-555-0192", ""),
              ("Free Test Call Co", "7 E Test St", "800-555-0193", "https://www.facebook.com/freetest")]
    with snap.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=cols)
        writer.writeheader()
        for name, street, phone, site in places:
            writer.writerow({"name": name, "street": street, "city": "Casa Grande", "zip": "85122", "phone": phone,
                             "website": site, "category": "", "date_seen": "2026-10-01"})
    growth.take_snapshot(snap, "overture stephanie", db_path=db)
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row

    def item(name, street):
        return {"name": name, "zip": "85122", "town": "casa grande", "signals": ["new_license"],
                "details": [{"signal": "new_license", "street": street}]}

    tenant = item("Grande Test Optometry", "1004 N Mall Parkway")  # alike to both tenants: neither
    exact = item("Grande Test Vision", "1004 N Mall Pkwy")  # an exact name settles it
    numberless = item("Grande Test Dental", "N Mall Pkwy")
    free = item("Free Test Call Co", "7 E Test St")
    _contacts(con, [tenant, exact, numberless, free])
    assert tenant["phone"] == "" and exact["phone"] == "(520) 555-0192" and numberless["phone"] == ""
    assert (free["phone"], free["website"]) == ("", "")  # toll-free and a Facebook page aren't the business's own
    con.execute("DROP TABLE snapshot_rows")
    kept = item("Grande Test Vision", "1004 N Mall Pkwy")
    _contacts(con, [kept])  # no listings: the license street still shows
    assert kept["address"].startswith("1004 N Mall Pkwy") and kept["phone"] == ""
    con.close()
