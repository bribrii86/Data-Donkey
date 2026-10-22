"""v0.6.8: building owners from the Pinal parcel layer (fake layer pages; made-up owners)."""
import csv
from datetime import date

import pytest

from donkey import api, parcels
from donkey.ingest import ingest

TODAY = date(2026, 10, 19)


def _parcel(pid, site, owner, city="", state="", year=0, sale="", use="Commercial"):
    return {"PARCELID": pid, "SITEADDRES": site, "OWNERNME1": owner, "OWNERNME2": " ", "PSTLCITY": city,
            "PSTLSTATE": state, "USEDSCRP": use, "RESYRBLT": year, "SALEDATE": sale, "SALEPRICE": 0}


PARCELS = [
    _parcel("1", "10 W TEST ST CASA GRANDE, AZ 85122", "SAMPLE TEST BURGERS INC", "IRVINE", "CA", 2010, "2010-03-01"),
    _parcel("2", "20 E TEST RD CASA GRANDE, AZ 85122", "LANDLORD TEST LLC", "DALLAS", "TX", 1999, "2024-05-01"),
    _parcel("3", "30 N TEST AVE CASA GRANDE, AZ 85122", "LOCAL TEST CAFE LLC", " ", " ", 1975),
    _parcel("4", "40 S MALL TEST PKWY CASA GRANDE, AZ 85122", "OWNER ONE LLC"),
    _parcel("5", "40 S MALL TEST PKWY CASA GRANDE, AZ 85122", "OWNER TWO LLC"),
]


def _fake_pages(pages):
    calls = []

    def fetch(url, data):
        calls.append(data)
        return pages[len(calls) - 1]
    return fetch, calls


def test_split_site():
    assert parcels.split_site("717 N PROMENADE PKWY CASA GRANDE, AZ 85194", ["Casa Grande", "Maricopa"]) == \
        ("717 N PROMENADE PKWY", "Casa Grande", "85194")
    assert parcels.split_site("5 A ST SOMEWHERE, AZ 85001", ["Casa Grande"]) == ("5 A ST SOMEWHERE", "", "85001")
    assert parcels.split_site("no zip here", []) == ("no zip here", "", "")


def test_pull_pages_and_replaces(tmp_path):
    db = tmp_path / "donkey.db"
    fetch, calls = _fake_pages([{"features": [{"attributes": p} for p in PARCELS[:2]], "exceededTransferLimit": True},
                                {"features": [{"attributes": p} for p in PARCELS[2:]]}])
    assert parcels.pull("stephanie", db, fetch=fetch)["parcels"] == 5
    assert [c["resultOffset"] for c in calls] == ["0", "2"] and "85122" in calls[0]["where"]
    assert "'Residential'" not in calls[0]["where"]
    fetch, _ = _fake_pages([{"features": [{"attributes": PARCELS[0]}]}])
    assert parcels.pull("stephanie", db, fetch=fetch)["parcels"] == 1  # replaced, not added
    fetch, _ = _fake_pages([{"error": {"message": "bad query"}}])
    with pytest.raises(RuntimeError, match="bad query"):
        parcels.pull("stephanie", db, fetch=fetch)


def test_call_this_week_gets_the_building_owner(tmp_path):
    db = tmp_path / "donkey.db"
    rows = [["Sample Test Burgers", "", "10 W Test St", "Casa Grande", "AZ", "85122", "BL-1", "2026-10-01", "Food"],
            ["Tenant Test Salon", "", "20 E Test Rd Ste 3", "Casa Grande", "AZ", "85122", "BL-2", "2026-10-01", "Svc"],
            ["Local Test Cafe", "", "30 N Test Ave", "Casa Grande", "AZ", "85122", "BL-3", "2026-10-01", "Food"],
            ["Split Test Shop", "", "40 S Mall Test Pkwy", "Casa Grande", "AZ", "85122", "BL-4", "2026-10-01", "Svc"],
            ["Nowhere Test Co", "", "99 Z Test Way", "Casa Grande", "AZ", "85122", "BL-5", "2026-10-01", "Svc"]]
    path = tmp_path / "lic.csv"
    with path.open("w", encoding="utf8", newline="") as stream:
        csv.writer(stream).writerows([["Business Name", "Legal Name", "Address", "City", "State", "ZIP",
                                       "License Number", "Issue Date", "Classification"], *rows])
    ingest("city-licenses", path, "test", db)
    hits = {h["name"]: h for h in api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=db)}
    assert all(h["property"] == {} for h in hits.values())  # no parcels pulled yet: nothing, no error
    fetch, _ = _fake_pages([{"features": [{"attributes": p} for p in PARCELS]}])
    parcels.pull("stephanie", db, fetch=fetch)
    hits = {h["name"]: h for h in api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=db)}
    burgers, salon, cafe = hits["Sample Test Burgers"], hits["Tenant Test Salon"], hits["Local Test Cafe"]
    assert burgers["property"]["owns_building"] and burgers["property"]["out_of_state"]
    assert burgers["locality"]["value"] == "corporate" and "Irvine, CA" in burgers["locality"]["evidence"]
    assert burgers["property_note"] == "owns its building: Sample Test Burgers Inc (Irvine, CA); built 2010; last sold 2010-03"
    assert salon["property"]["out_of_state"] and not salon["property"]["owns_building"]
    assert salon["locality"]["value"] != "corporate"  # an out-of-state landlord says nothing about the tenant
    assert salon["property_note"].startswith("building owner: Landlord Test Llc (Dallas, TX)")
    assert cafe["property"]["owns_building"] and not cafe["property"]["out_of_state"]  # blank mailing state
    assert hits["Split Test Shop"]["property"] == {} and hits["Nowhere Test Co"]["property"] == {}
