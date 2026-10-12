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


def test_chain_store_and_address_spelling_are_not_moves():
    """v0.5.7: the 2026-10-11 Overture diff called 235 businesses moved; most were a chain's other store or the
    same address spelled differently."""
    old = [
        {"name": "Circle K", "street": "1776 E McCartney Rd", "city": "Casa Grande", "zip": "85122",
         "phone": "520-836-5157", "category": "gas station"},
        {"name": "Shoe Dept. Encore", "street": "1004 N Promenade Pkwy, Promenade At Casa Grande, Ste 141",
         "city": "Casa Grande", "zip": "85122", "phone": "520-836-1464", "category": "shoe store"},
        {"name": "Hanger Clinic", "street": "1515 East Florence Blvd Suite 107", "city": "Casa Grande",
         "zip": "85122", "phone": "520-555-0110", "category": "clinic"},
        {"name": "Tena Transport", "street": "1146 Jimmie Kerr Blvd", "city": "Casa Grande", "zip": "85122",
         "phone": "520-555-0111", "category": "trucking"},
        {"name": "Sun Life Health", "street": "865 N Arizola Rd", "city": "Casa Grande", "zip": "85122",
         "phone": "520-836-3446", "category": "clinic"},
    ]
    new = [
        old[0],  # the first store is still there
        {**old[0], "street": "1415 E Florence Blvd", "phone": "520-426-9328"},  # a second store, new phone
        {**old[1], "street": "1004 N Promenade Pkwy #141"},
        {**old[2], "street": "1515 E Florence Blvd"},
        {**old[3], "street": "1146 E Jimmie Kerr Blvd"},
        {**old[4], "street": "1863 E Florence Blvd", "phone": "888-381-3446"},  # the old site is gone: a move
    ]
    kinds = {(c["after"]["name"], c["after"]["street"], c["kind"]) for c in growth.diff_rows(old, new)}
    assert kinds == {
        ("Circle K", "1415 E Florence Blvd", "growth_new_business"),
        ("Sun Life Health", "1863 E Florence Blvd", "growth_moved"),
        ("Sun Life Health", "1863 E Florence Blvd", "growth_new_phone"),
    }


def test_redo_diffs_recomputes_stored_events(tmp_path, monkeypatch, capsys):
    db = tmp_path / "donkey.db"
    monkeypatch.setenv("DONKEY_DB", str(db))
    growth.take_snapshot(_csv(tmp_path / "w1.csv", WEEK1), "t", "2026-10-05", db_path=db)
    growth.take_snapshot(_csv(tmp_path / "w2.csv", WEEK2), "t", "2026-10-12", db_path=db)
    growth.diff_snapshots(db_path=db, event_date="2026-10-13")
    assert main(["growth-diff", "--redo-all"]) == 0 and "5 changes" in capsys.readouterr().out
    assert main(["growth-diff", "--redo-all"]) == 0  # again: same events, not doubled
    events = api.events("growth_moved", ["Casa Grande"], db_path=db)
    assert len(events) == 1 and events[0]["event_date"] == "2026-10-13"  # keeps the original event date


def test_same_signal_twice_counts_once_on_call_this_week(tmp_path):
    db = tmp_path / "donkey.db"
    stores = [{"name": "Dutch Bros Coffee", "street": s, "city": "Casa Grande", "zip": "85122",
               "phone": f"520-555-020{n}", "website": "", "category": "coffee_shop", "date_seen": "2026-10-12"}
              for n, s in enumerate(("1 Main St", "2 Pinal Ave", "3 Hunt Hwy"))]  # three stores, three events
    growth.take_snapshot(_csv(tmp_path / "w1.csv", WEEK1), "t", "2026-10-05", db_path=db)
    growth.take_snapshot(_csv(tmp_path / "w2.csv", stores), "t", "2026-10-12", db_path=db)
    growth.diff_snapshots(db_path=db)
    (hit,) = [h for h in api.call_this_week("stephanie", as_of="2026-10-12", db_path=db)
              if h["name"] == "Dutch Bros Coffee"]
    assert hit["score"] == 3 and hit["reasons"] == ["new business on the list"]


def test_same_name_zip_match_prefers_the_same_address():
    old = [{"name": "Banner Health", "street": s, "city": "Casa Grande", "zip": "85122", "phone": p,
            "category": "clinic"} for s, p in (("1811 E McMurray Blvd", "520-555-0301"),
                                               ("3235 N Hunt Hwy", "520-555-0302"))]
    new = [{**old[1], "phone": "520-555-0402"}, {**old[0], "phone": "520-555-0401"}]  # both phones changed
    kinds = sorted(c["kind"] for c in growth.diff_rows(old, new))
    assert kinds == ["growth_new_phone", "growth_new_phone"]  # no false moves, whatever the row order


def test_redo_diffs_keeps_overture_scores_and_is_all_or_nothing(tmp_path, monkeypatch):
    from donkey import overture

    db = tmp_path / "donkey.db"
    moved = [{**WEEK1[1], "street": "2000 N Trekell Rd Ste 9"}]
    growth.take_snapshot(_csv(tmp_path / "o1.csv", WEEK1[1:2]), "overture stephanie", "2026-08-19", db_path=db)
    growth.take_snapshot(_csv(tmp_path / "o2.csv", moved), "overture stephanie", "2026-09-23", db_path=db)
    growth.diff_snapshots(db_path=db, event_date="2026-10-11", scores=overture.SCORES)

    def broken(*args):
        raise RuntimeError("disk full")

    monkeypatch.setattr(growth, "_diff", broken)
    try:
        growth.redo_diffs(db_path=db)
    except RuntimeError:
        pass
    monkeypatch.undo()
    (event,) = api.events("growth_moved", ["Casa Grande"], db_path=db)  # the failed redo kept the old diff
    growth.redo_diffs(db_path=db)
    (event,) = api.events("growth_moved", ["Casa Grande"], db_path=db)
    assert '"score": 2' in event["details_json"] and event["event_date"] == "2026-10-11"  # Overture's lower score


def test_new_store_listed_first_does_not_claim_the_old_store():
    old = [{"name": "Circle K", "street": "1776 E McCartney Rd", "city": "Casa Grande", "zip": "85122",
            "phone": "520-836-5157", "category": "gas station"}]
    new = [{**old[0], "street": "1415 E Florence Blvd", "phone": "520-426-9328"},  # the new store, listed first
           {**old[0], "phone": "520-836-0000"}]  # the old store, its phone changed
    kinds = {(c["after"]["street"], c["kind"]) for c in growth.diff_rows(old, new)}
    assert kinds == {("1415 E Florence Blvd", "growth_new_business"), ("1776 E McCartney Rd", "growth_new_phone")}


def test_kiosks_do_not_score_but_government_offices_do(tmp_path):
    """v0.5.8: Buck 2026-10-12: a Coinme kiosk has no office to sell to; Pinal County Planning does."""
    db = tmp_path / "donkey.db"
    names = ("Coinme", "Bitcoin Depot | Bitcoin ATM", "Redbox", "ecoATM", "Amazon Locker - Greek", "Chase ATM",
             "Pinal County Planning & Development", "Atmosphere Salon")
    rows = [{"name": name, "street": f"{n} Main St", "city": "Casa Grande", "zip": "85122",
             "phone": f"520-555-03{n:02d}", "website": "", "category": "", "date_seen": "2026-10-12"}
            for n, name in enumerate(names, 1)]
    growth.take_snapshot(_csv(tmp_path / "w1.csv", WEEK1), "t", "2026-10-05", db_path=db)
    growth.take_snapshot(_csv(tmp_path / "w2.csv", rows), "t", "2026-10-12", db_path=db)
    growth.diff_snapshots(db_path=db)
    listed = {h["name"] for h in api.call_this_week("stephanie", as_of="2026-10-12", db_path=db)}
    assert {"Pinal County Planning & Development", "Atmosphere Salon"} <= listed
    assert not listed & set(names[:6])
