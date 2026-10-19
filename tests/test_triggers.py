"""v0.5.2: areas, UCC monthly index ingest with lessor tags, and call-this-week scoring. Synthetic rows only."""
import csv
import json
import sqlite3

import pytest

from donkey import api, areas, growth, ucc
from donkey.ingest import ingest, main

INDEX_HEADER = ["FILE_NUMBER", "FILING_DATE", "DEBTOR_NAME", "Debtor City", "DEBTOR_ZIP", "SECURED_PARTY_NAME",
                "LIEN_STATUS", "Debtor Fax"]
INDEX_ROWS = [
    # new this month, copier lessor, in Stephanie's territory
    ["202600000001", "10/01/2026", "Synthetic Dental PLLC", "Casa Grande", "85122", "GreatAmerica Financial Services",
     "Active", ""],
    # filed 56 months before 2026-10-12: estimated lease end 2027-02, inside the 180-day lead
    ["202100000002", "02/15/2022", "Example Law Office", "Maricopa", "85138", "De Lage Landen Financial Services Inc",
     "Active", ""],
    # bank lien this month in Phoenix: outside Stephanie's area, 1 point only
    ["202600000003", "10/05/2026", "Sample Roofing LLC", "Phoenix", "85004", "Example National Bank", "Active", ""],
    # merchant cash advance: scores nothing
    ["202600000004", "10/06/2026", "Test Cafe", "Eloy", "85131", "Quick Funding LLC", "Active", ""],
]


def _tsv(path, header, rows):
    with path.open("w", encoding="utf8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t")
        writer.writerow(header)
        writer.writerows(rows)
    return path


@pytest.mark.parametrize("zip_code, city, area, inside", [
    ("85122", "", "stephanie", True),
    ("", "Casa Grande", "stephanie", True),
    ("85140", "San Tan Valley", "stephanie", False),  # Pinal, but not her towns
    ("85140", "", "pinal", True),
    ("85004", "Phoenix", "phoenix-metro", True),
    ("85719", "Tucson", "tucson-metro", True),
    ("85719", "Tucson", "phoenix-metro", False),
    ("86001", "Flagstaff", "arizona", True),
    ("85122-1234", "", "stephanie", True),
])
def test_areas(zip_code, city, area, inside):
    assert areas.contains(area, zip_code, city) is inside


def test_areas_file_override_and_unknown(tmp_path, monkeypatch):
    assert {"stephanie", "pinal", "phoenix-metro", "tucson-metro", "arizona"} <= set(areas.names())
    custom = tmp_path / "areas.json"
    custom.write_text(json.dumps({"dealer2": {"zips": ["85701"]}}), encoding="utf8")
    monkeypatch.setenv("DONKEY_AREAS", str(custom))
    assert areas.names() == ["dealer2"] and areas.contains("dealer2", "85701")
    with pytest.raises(ValueError, match="unknown area"):
        areas.load("stephanie")


@pytest.mark.parametrize("party, kind", [
    ("GREATAMERICA FINANCIAL SERVICES CORPORATION", "copier_lessor"),
    ("De Lage Landen Financial Services, Inc.", "copier_lessor"),
    ("Canon Financial Services, Inc.", "copier_lessor"),
    ("Wells Fargo Vendor Financial Services, LLC", "copier_lessor"),
    ("Navitas Credit Corp", "equipment_finance"),
    ("Deere & Company", "ag"),
    ("Corporation Service Company, as Representative", "mca"),
    ("Western Alliance Bank", "bank"),
    ("Internal Revenue Service", "government"),
    ("U.S. BANK EQUIPMENT FINANCE", "copier_lessor"),
    ("FLEXPRINT, LLC", "copier_lessor"),
    ("C T CORPORATION SYSTEM, AS REPRESENTATIVE", "mca"),
    ("KS STATEBANK", "bank"),
    ("CAPITAL ONE PUBLIC FUNDING, LLC", "bank"),
    ("U.S. SMALL BUSINESS ADMINISTRATION", "government"),
    ("Some Person", "other"),
    ("", ""),
])
def test_classify(party, kind):
    assert ucc.classify(party) == kind


def test_enrich_lease_window():
    tags = ucc.enrich({"secured_party": "GreatAmerica", "event_date": "2026-01-31"})
    assert tags == {"lessor_type": "copier_lessor", "lease_window_start": "2029-01-28", "lease_end_est": "2031-01-28"}
    assert ucc.enrich({"secured_party": "Example Bank", "event_date": "2026-01-31"})["lease_end_est"] == ""


def test_index_ingest_maps_unknown_headers_and_tags(tmp_path):
    db = tmp_path / "donkey.db"
    index = _tsv(tmp_path / "ucc-index-2026-10.txt", INDEX_HEADER, INDEX_ROWS)
    result = ingest("az-sos-ucc-index", index, "AZ SOS (synthetic)", db)
    assert result["row_count"] == 4 and result["new"] == 4
    assert set(result["unmapped"]) == {"street", "state", "lapse_date"}  # not in this header; add names to the map
    filings = {e["business_name"]: json.loads(e["details_json"]) for e in api.events("ucc_filing", db_path=db)}
    dental = filings["Synthetic Dental PLLC"]
    assert dental["lessor_type"] == "copier_lessor" and dental["zip"] == "85122"
    assert dental["filing_number"] == "202600000001" and dental["lease_end_est"] == "2031-10-01"
    assert filings["Test Cafe"]["lessor_type"] == "mca"
    assert api.events("ucc_filing", ["Casa Grande"], since="2026-10-01", db_path=db)[0]["event_date"] == "2026-10-01"


def _snapshots(tmp_path, db):
    cols = ["name", "street", "city", "zip", "phone", "website", "category", "date_seen"]
    week1 = [{"name": "Desert Rose Plumbing", "street": "123 W Sunny Ln", "city": "Maricopa", "zip": "85138",
              "phone": "520-555-0101", "website": "", "category": "Plumber", "date_seen": "2026-10-03"},
             {"name": "Valley Sign Co", "street": "9 N 1st St Ste 2", "city": "Phoenix", "zip": "85004",
              "phone": "602-555-0109", "website": "", "category": "signs", "date_seen": "2026-10-03"}]
    week2 = [{**week1[0], "street": "450 N Pinal Ave Suite 110", "city": "Casa Grande", "zip": "85122",
              "date_seen": "2026-10-10"},
             {**week1[1], "street": "77 E Van Buren St Ste 5", "date_seen": "2026-10-10"}]
    for name, rows in (("w1.csv", week1), ("w2.csv", week2)):
        with (tmp_path / name).open("w", encoding="utf8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=cols)
            writer.writeheader()
            writer.writerows(rows)
        growth.take_snapshot(tmp_path / name, "arizona directory", db_path=db)
    growth.diff_snapshots(db_path=db)


def test_call_this_week_combines_triggers_by_area(tmp_path):
    db = tmp_path / "donkey.db"
    ingest("az-sos-ucc-index", _tsv(tmp_path / "idx.txt", INDEX_HEADER, INDEX_ROWS), "AZ SOS (synthetic)", db)
    _snapshots(tmp_path, db)

    hits = api.call_this_week("stephanie", as_of="2026-10-12", db_path=db)
    assert [(h["name"], h["score"], h["signals"]) for h in hits] == [
        ("Example Law Office", 6, ["ucc_lease_ending", "print_heavy"]),  # v0.6.2: law and dental offices +1
        ("Desert Rose Plumbing", 5, ["home_to_office"]),
        ("Synthetic Dental PLLC", 5, ["ucc_new_copier_lessor", "print_heavy"]),
    ]
    assert hits[0]["reasons"] == ["copier lease likely ending", "heavy-print line of work"]

    phoenix = api.call_this_week("phoenix-metro", as_of="2026-10-12", min_score=1, db_path=db)
    assert [(h["name"], h["signals"]) for h in phoenix] == [
        ("Valley Sign Co", ["moved"]), ("Sample Roofing LLC", ["ucc_new_bank"])]
    assert len(api.call_this_week(None, as_of="2026-10-12", db_path=db)) == 4  # statewide, score 3+
    assert api.growth(area="phoenix-metro", db_path=db)[0]["name"] == "Valley Sign Co"


def test_call_this_week_scorer_hook_adds_to_the_same_business(tmp_path):
    db = tmp_path / "donkey.db"
    ingest("az-sos-ucc-index", _tsv(tmp_path / "idx.txt", INDEX_HEADER, INDEX_ROWS), "AZ SOS (synthetic)", db)

    def permits(con, area, as_of, since):  # an app-supplied trigger, e.g. a build-out permit feed
        yield {"name": "Synthetic Dental, PLLC", "zip": "85122", "town": "Casa Grande", "score": 4,
               "signal": "buildout_permit", "reason": "build-out permit filed", "date": "2026-10-08"}

    hits = api.call_this_week("stephanie", as_of="2026-10-12", scorers=[permits], db_path=db)
    assert hits[0]["name"] == "Synthetic Dental PLLC" and hits[0]["score"] == 9
    assert hits[0]["reasons"] == ["new financing with a copier lessor", "build-out permit filed", "heavy-print line of work"]


def test_snapshot_area_filter_and_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    source = tmp_path / "statewide.csv"
    source.write_text("name,street,city,zip,phone,website,category,date_seen\n"
                      "A Dental,1 Main St Ste 1,Casa Grande,85122,520-555-0001,,dentist,2026-10-10\n"
                      "B Dental,2 Central Ave Ste 2,Phoenix,85004,602-555-0002,,dentist,2026-10-10\n",
                      encoding="utf8")
    assert main(["growth-snapshot", "--file", str(source), "--label", "cg", "--area", "stephanie"]) == 0
    assert "1 rows" in capsys.readouterr().out
    assert main(["growth-snapshot", "--file", str(source), "--label", "phx", "--area", "phoenix-metro"]) == 0
    assert "1 rows" in capsys.readouterr().out  # same file, other area: stored as its own snapshot
    index = _tsv(tmp_path / "idx.txt", INDEX_HEADER, INDEX_ROWS)
    assert main(["ingest", "--source", "az-sos-ucc-index", "--file", str(index), "--received-from", "AZ SOS"]) == 0
    assert "not found in this file" in capsys.readouterr().out
    assert main(["call-this-week", "--area", "stephanie", "--as-of", "2026-10-12"]) == 0
    out = capsys.readouterr().out
    assert "Example Law Office" in out and "2 to call" in out
    assert main(["call-this-week", "--area", "nowhere"]) == 1


# Same columns as the SOS CSV export (data/ucc.csv in Sales-Pilot): no city, no ZIP. Synthetic rows.
SOS_CSV = ("Debtor,Filing Date,File Number,Secured Party\n"
           "CASA GRANDE SYNTHETIC CLINIC LLC,10/01/2026,202600000011,GreatAmerica Financial Services\n"
           "EXAMPLE OFFICE SUPPLY LLC,10/02/2026,202600000012,De Lage Landen Financial Services Inc\n"
           "MARICOPA COUNTY SAMPLE DISTRICT,10/03/2026,202600000013,GreatAmerica Financial Services\n"
           "NO PARTY HOLDINGS LLC,10/04/2026,202600000014,\n")


def test_sos_csv_rows_stay_in_area_runs(tmp_path):
    db = tmp_path / "donkey.db"
    csv_file = tmp_path / "ucc.csv"
    csv_file.write_text(SOS_CSV, encoding="utf8")
    ingest("az-sos-ucc", csv_file, "AZ SOS (synthetic)", db)
    statewide = api.call_this_week(None, as_of="2026-10-12", db_path=db)
    assert len(statewide) == 3
    for area in ("stephanie", "arizona", "phoenix-metro"):
        hits = {h["name"]: h for h in api.call_this_week(area, as_of="2026-10-12", db_path=db)}
        assert set(hits) == {h["name"] for h in statewide}, area
    stephanie = {h["name"]: h for h in api.call_this_week("stephanie", as_of="2026-10-12", db_path=db)}
    clinic = stephanie["CASA GRANDE SYNTHETIC CLINIC LLC"]
    assert clinic["town"] == "Casa Grande" and clinic["details"][0]["location"] == "debtor name"
    county = stephanie["MARICOPA COUNTY SAMPLE DISTRICT"]  # a county, not the town of Maricopa
    assert county["town"] == "" and county["details"][0]["location"] == "unknown"


def test_database_from_v050_gets_backfilled(tmp_path, monkeypatch, capsys):
    db = tmp_path / "donkey.db"
    monkeypatch.setenv("DONKEY_DB", str(db))
    csv_file = tmp_path / "ucc.csv"
    csv_file.write_text(SOS_CSV, encoding="utf8")
    ingest("az-sos-ucc", csv_file, "AZ SOS (synthetic)", db)
    con = sqlite3.connect(db)  # rewrite the events the way v0.5.0 stored them: no tags
    for event_id, details in con.execute("SELECT id, details_json FROM events").fetchall():
        old = {k: v for k, v in json.loads(details).items() if k in {"filing_number", "secured_party"}}
        con.execute("UPDATE events SET details_json = ? WHERE id = ?", (json.dumps(old), event_id))
    con.commit()
    con.close()
    # read-only path tags on the fly
    assert len(api.call_this_week("stephanie", as_of="2026-10-12", db_path=db)) == 3
    # re-ingesting the same file is skipped by hash, but opening the database stores the tags
    assert main(["ingest", "--source", "az-sos-ucc", "--file", str(csv_file), "--received-from", "AZ SOS"]) == 0
    assert "Already ingested" in capsys.readouterr().out
    tags = [json.loads(e["details_json"]) for e in api.events("ucc_filing", db_path=db)]
    assert sorted(t["lessor_type"] for t in tags) == ["", "copier_lessor", "copier_lessor", "copier_lessor"]
    assert all(t["lease_end_est"] for t in tags if t["lessor_type"] == "copier_lessor")
    assert main(["call-this-week", "--area", "stephanie", "--as-of", "2026-10-12"]) == 0
    out = capsys.readouterr().out
    assert "3 to call" in out and "(location unknown)" in out and "(Casa Grande)" in out


def test_lease_calendar_by_month(tmp_path, capsys, monkeypatch):
    """v0.6.2: copier leases likely ending, soonest first, with their month; other lessors only on request."""
    db = tmp_path / "donkey.db"
    rows = INDEX_ROWS + [["202200000005", "06/10/2022", "Sample Title Agency", "Coolidge", "85128",
                          "Ascentium Capital LLC", "Active", ""]]
    ingest("az-sos-ucc-index", _tsv(tmp_path / "idx.txt", INDEX_HEADER, rows), "AZ SOS (synthetic)", db)
    cal = api.lease_calendar("stephanie", as_of="2026-10-12", months=12, db_path=db)
    assert [(r["name"], r["month"], r["lessor_type"]) for r in cal] == [
        ("Example Law Office", "2027-02", "copier_lessor")]
    assert cal[0]["filed"] == "2022-02-15" and cal[0]["who_decides"] == "unknown"
    wide = api.lease_calendar("stephanie", as_of="2026-10-12", months=12, all_lessors=True, db_path=db)
    assert [r["name"] for r in wide] == ["Example Law Office", "Sample Title Agency"]
    assert api.lease_calendar("stephanie", as_of="2026-10-12", months=3, db_path=db) == []
    with pytest.raises(ValueError, match="months"):
        api.lease_calendar("stephanie", months=0, db_path=db)
    monkeypatch.setenv("DONKEY_DB", str(db))
    out = tmp_path / "cal.csv"
    assert main(["lease-calendar", "--as-of", "2026-10-12", "--csv", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "2027-02" in printed and "Example Law Office (Maricopa)" in printed and "1 leases likely ending" in printed
    assert "Example Law Office" in out.read_text(encoding="utf8")


@pytest.mark.parametrize("name, category, heavy", [
    ("Pinal Family Law PLLC", "", True), ("Desert CPA Group", "", True), ("Valley Realty", "", True),
    ("Sun Dental", "", True), ("Casa Grande Prep Academy", "", True), ("Acme LLC", "accounting", True),
    ("Desert Rose Plumbing", "plumber", False), ("Title Boxing Club", "", False), ("Maricopa Tacos", "", False),
    ("Acme", "insurance_agency", True), ("Acme", "elementary_school", True), ("Acme", "real_estate_agent", True),
    ("Acme", "tax_services_and_computation", True), ("Casa Grande Union High School", "", True),
    ("Church's Chicken", "", False), ("Academy Sports", "", False), ("Charter Communications", "", False),
    ("University Pizza", "", False), ("Truck Driving School", "", False), ("Law Enforcement Supply", "", False),
    ("Green Lawn Care", "", False), ("Grace Church", "church", False),
])
def test_print_heavy_names(name, category, heavy):
    from donkey.triggers import looks_print_heavy
    assert looks_print_heavy(name, category) is heavy


def test_hiring_boost_from_who_decides_answer(tmp_path):
    from donkey import locality
    from donkey.ingest import connect
    db = tmp_path / "donkey.db"
    _snapshots(tmp_path, db)
    con = connect(db)
    locality.save(con, "Desert Rose Plumbing", "85122", "Casa Grande", {
        "value": "local", "evidence": "owner on site", "confidence": "high", "owner_hint": "",
        "initiative": "Hiring a front desk coordinator (Indeed post)", "source": "agent"})
    con.commit()
    con.close()
    hit = api.call_this_week("stephanie", as_of="2026-10-12", db_path=db)[0]
    assert hit["name"] == "Desert Rose Plumbing" and hit["score"] == 7
    assert hit["signals"] == ["home_to_office", "hiring"] and hit["reasons"][-1] == "hiring or expanding"


@pytest.mark.parametrize("initiative, hiring", [
    ("Hiring two front desk staff", True), ("job posts for admin help", True), ("opening another office", True),
    ("not hiring right now", False), ("no expansion plans this year", False), ("opening about spring", False),
])
def test_hiring_words(initiative, hiring):
    from donkey.triggers import _boost
    item = {"name": "Acme", "score": 3, "signals": [], "reasons": [], "locality": {"initiative": initiative}}
    _boost(item, "plumber")
    assert ("hiring" in item["signals"]) is hiring


def test_lease_calendar_drops_repeats_and_dead_filings(tmp_path):
    db = tmp_path / "donkey.db"
    rows = INDEX_ROWS + [["202200000006", "02/20/2022", "Example Law Office", "Maricopa", "85138",
                          "De Lage Landen Financial Services Inc", "Active", ""],
                         ["202200000007", "03/01/2022", "Sample Insurance Agency", "Maricopa", "85138",
                          "GreatAmerica Financial Services", "Terminated", ""]]
    ingest("az-sos-ucc-index", _tsv(tmp_path / "idx.txt", INDEX_HEADER, rows), "AZ SOS (synthetic)", db)
    cal = api.lease_calendar("stephanie", as_of="2026-10-12", months=12, db_path=db)
    assert [r["name"] for r in cal] == ["Example Law Office"]
