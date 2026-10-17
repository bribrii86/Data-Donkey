"""v0.6.0: who decides (locality), board minutes and Overture brands. Synthetic rows, fake NPI and Console clients; no
network, no paid calls."""
import json
from datetime import datetime, timedelta, timezone

from donkey import api, locality, overture, runner
from donkey.ingest import connect, ingest, main

from test_runner import FakeClient, _settings

TODAY = datetime.now(timezone.utc).date()
EFFECTIVE = (TODAY - timedelta(days=10)).isoformat()
HEADER = "name,street,city,zip,address_type,agent,entity_id,entity_type,effective_date,newspaper,source_url,date_seen\n"


def _notices(tmp_path, *names):
    """New-LLC events (3 points each) so the businesses reach call-this-week."""
    path = tmp_path / "notices.csv"
    rows = "".join(f"{name},1 Main St,Casa Grande,85122,business,Agent,{n},LLC,{EFFECTIVE},Dispatch,,{TODAY}\n"
                   for n, name in enumerate(names, 1))
    path.write_text(HEADER + rows, encoding="utf8")
    ingest("pinal-notices", path, "test", tmp_path / "donkey.db")
    return tmp_path / "donkey.db"


def _npi(results):
    return lambda url: {"result_count": len(results), "results": results}


def _npi_org(name, zip_code, street, title="Owner", subpart="NO"):
    return {"number": "1" + zip_code + str(len(street)),
            "basic": {"organization_name": name.upper(), "organizational_subpart": subpart,
                      "authorized_official_first_name": "JANE", "authorized_official_last_name": "ROE",
                      "authorized_official_title_or_position": title},
            "addresses": [{"address_purpose": "LOCATION", "address_1": street, "state": "AZ", "postal_code": zip_code}]}


def test_free_rules():
    brands = locality.load_brands()
    value = lambda name, **kw: locality.classify(name, brands=brands, **kw)["value"]  # noqa: E731
    assert value("City of Eloy") == "central_procurement"
    assert value("Casa Grande Elementary School District") == "central_procurement"
    assert value("Pinal County Federal Credit Union") != "central_procurement"  # a credit union, not the county
    assert value("Walmart Supercenter") == "corporate" and value("Dollar General #4411") == "corporate"
    assert value("Subway") == "franchise" and value("H&R Block") == "franchise"
    # a one-word brand needs a store word after it, so a different business sharing the word stays unknown
    assert value("Target Pest Control") == "unknown" and value("Shell Plumbing LLC") == "unknown"
    # ...unless Overture tags the place with that brand
    assert value("Freddy's Frozen Custard & Steakburgers", brand="Freddy's") == "franchise"
    assert value("Copper Mesa Dental") == "unknown"
    assert value("Copper Mesa Dental", locations=3) == "regional"
    assert value("Copper Mesa Dental", locations=9) == "corporate"
    answer = locality.classify("Walmart", brands=brands)
    assert answer["confidence"] == "high" and "Walmart" in answer["evidence"]


def test_npi_check_counts_locations_and_reads_the_owner():
    group = [_npi_org("Sunrise Dental", z, f"{n} Main St") for n, z in enumerate(["85122", "85138", "85132"])]
    answer = locality.npi_check("Sunrise Dental PLLC", _npi(group))
    assert answer["value"] == "regional" and "3 Arizona locations" in answer["evidence"]
    solo = locality.npi_check("Copper Mesa Dental", _npi([_npi_org("Copper Mesa Dental", "85122", "9 W Main",
                                                                    title="Owner/DDS")]))
    assert solo["value"] == "local" and solo["owner_hint"] == "Jane Roe, Owner/DDS (NPI)"
    many = [_npi_org("Big Smile Dental", "85122", f"{n} Oak St") for n in range(9)]
    assert locality.npi_check("Big Smile Dental", _npi(many))["value"] == "corporate"
    other = [_npi_org("Unrelated Clinic", "85122", "1 A St")]
    assert locality.npi_check("Copper Mesa Dental", _npi(other)) is None


def test_check_rules_stores_and_keeps_paid_answers(tmp_path):
    db = tmp_path / "donkey.db"
    items = [{"name": "Dollar General", "zip": "85122", "town": "Casa Grande"},
             {"name": "Copper Mesa Dental", "zip": "85122", "town": "Casa Grande"},
             {"name": "Gudelia LLC", "zip": "85122", "town": "Casa Grande"}]
    fetch = _npi([_npi_org("Copper Mesa Dental", "85122", "9 W Main", title="President")])
    done = locality.check_rules(items, db, TODAY, fetch=fetch, pause=0)
    assert done["counts"] == {"corporate": 1, "local": 1, "unknown": 1}
    con = connect(db)
    locality.save(con, "Gudelia LLC", "85122", "Casa Grande", {"value": "local", "evidence": "owner on site",
                                                               "confidence": "med", "source": "agent"})
    con.commit()
    con.close()
    again = locality.check_rules(items, db, TODAY, fetch=fetch, pause=0)
    assert again["counts"] == {"cached": 3}
    con = connect(db)
    row = dict(con.execute("SELECT * FROM locality WHERE name = 'Copper Mesa Dental'").fetchone())
    con.close()
    assert row["source"] == "npi" and row["owner_hint"].startswith("Jane Roe") and json.loads(row["details_json"])


def test_npi_down_is_a_note(tmp_path):
    def broken(url):
        raise OSError("registry down")

    done = locality.check_rules([{"name": "Valley Pediatrics", "zip": "85122", "town": "Casa Grande"}],
                                tmp_path / "d.db", TODAY, fetch=broken, pause=0)
    assert done["counts"] == {"unknown": 1} and "registry down" in done["notes"][0]


def test_call_this_week_carries_who_decides(tmp_path):
    db = _notices(tmp_path, "Walmart Supercenter", "Gudelia LLC")
    hits = {h["name"]: h for h in api.call_this_week("stephanie", as_of=TODAY, db_path=db)}
    assert hits["Walmart Supercenter"]["locality"]["value"] == "corporate"
    assert hits["Gudelia LLC"]["locality"]["value"] == "unknown"
    assert hits["Gudelia LLC"]["board_note"] == ""


def test_locality_agent_checks_only_unknown_rows_then_caches(tmp_path):
    db = _notices(tmp_path, "Walmart Supercenter", "Gudelia LLC", "Pathmaker Strategies LLC")
    key = locality.key("Gudelia LLC", "85122", "Casa Grande")
    answer = ("key,name,zip,decision_locality,evidence,confidence,owner_hint,owner_source,initiative,source_url\n"
              f"{key},Gudelia LLC,85122,regional,Site lists 3 Pinal offices,med,Maria Gudelia,ACC member,"
              "Opening a Maricopa office,https://gudelia.test\n"
              "not-a-key,Injected Row,85122,corporate,x,high,,,,\n").encode()
    client = FakeClient({"locality-01.csv": answer}, cost="80")
    result = runner.run_locality(_settings(tmp_path), client, db_path=db, sleep=lambda s: None, npi_fetch=_npi([]))
    assert result["status"] == "done" and result["checked"] == 2 and result["rows"] == 1  # Walmart settled free
    message = client.calls[1][1]
    assert "Gudelia LLC" in message and "Pathmaker" in message and "Walmart" not in message
    assert client.calls[0][3] == 500  # own $5 cap
    hits = {h["name"]: h for h in api.call_this_week("stephanie", as_of=TODAY, db_path=db)}
    got = hits["Gudelia LLC"]["locality"]
    assert got["value"] == "regional" and got["source"] == "agent" and "gudelia.test" in got["evidence"]
    assert got["owner_hint"] == "Maria Gudelia (ACC member)" and got["initiative"] == "Opening a Maricopa office"
    assert "Injected Row" not in hits

    # Pathmaker came back without an answer: still unknown, so it is asked again; Gudelia is cached
    second = FakeClient({}, cost="10")
    result = runner.run_locality(_settings(tmp_path), second, db_path=db, sleep=lambda s: None, npi_fetch=_npi([]))
    assert result["checked"] == 1 and "Gudelia" not in second.calls[1][1]


def test_locality_nothing_to_check_spends_nothing(tmp_path):
    db = _notices(tmp_path, "Walmart Supercenter")
    client = FakeClient({})
    result = runner.run_locality(_settings(tmp_path), client, db_path=db, sleep=lambda s: None, npi_fetch=_npi([]))
    assert result["status"] == "nothing_to_check" and client.calls == []


BODIES = ("body_name,body_type,zip_or_area,portal_platform,agendas_url,minutes_url\n"
          "Casa Grande Elementary School District,school district,85122,BoardDocs,https://board.test/a,"
          "https://board.test/m\n"
          "No Links Board,city,85122,none,,\n")
MINUTES = (f"body_name,town,meeting_date,doc_type,doc_url,flag,summary,keywords,vendor\n"
           f"Casa Grande Elementary School District,Casa Grande,{EFFECTIVE},minutes,https://board.test/d1,intent_plus,"
           f"Board approved an RFP for district copiers,copier;RFP,\n"
           f"City of Florence,Florence,{EFFECTIVE},agenda,https://board.test/d2,weird_flag,Discussed IT budget,IT,\n"
           f",Eloy,{EFFECTIVE},agenda,https://board.test/d3,info,No body name,IT,\n").encode()


def test_minutes_job_needs_a_body_list(tmp_path):
    client = FakeClient({})
    result = runner.run_minutes(_settings(tmp_path), client, db_path=tmp_path / "d.db", sleep=lambda s: None)
    assert result["status"] == "no_bodies" and client.calls == []


def test_minutes_become_board_events_scored_once(tmp_path):
    (tmp_path / "bodies.csv").write_text(BODIES, encoding="utf8")
    db = tmp_path / "donkey.db"
    client = FakeClient({"minutes-01.csv": MINUTES}, cost="120")
    result = runner.run_minutes(_settings(tmp_path), client, db_path=db, sleep=lambda s: None)
    assert result["status"] == "done" and result["rows"] == 2 and result["bodies"] == 1
    assert client.calls[0][3] == 300 and "board.test/a" in client.calls[1][1] and "No Links" not in client.calls[1][1]
    hits = {h["name"]: h for h in api.call_this_week("stephanie", as_of=TODAY, db_path=db)}
    board = hits["Casa Grande Elementary School District"]
    assert board["score"] == 4 and board["signals"] == ["board_intent_plus"]
    assert board["locality"]["value"] == "central_procurement"
    assert "RFP for district copiers" in board["board_note"]
    assert "City of Florence" not in hits  # an info flag (unknown flags become info) scores 0

    again = runner.run_minutes(_settings(tmp_path), FakeClient({"minutes-01.csv": MINUTES}), db_path=db,
                               sleep=lambda s: None)
    assert again["rows"] == 0  # the same documents are stored once


def test_import_brands_and_open_list_matching(tmp_path, monkeypatch, capsys):
    nsi = {"nsi": {"brands/shop/hardware": {"items": [
        {"displayName": "Copper State Bolt & Nut", "tags": {"brand": "Copper State Bolt & Nut",
                                                             "brand:wikidata": "Q1"}},
        {"displayName": "Ace", "tags": {"brand": "Ace"}}]}}}
    path = tmp_path / "nsi.json"
    path.write_text(json.dumps(nsi), encoding="utf8")
    db = tmp_path / "donkey.db"
    assert locality.import_brands(path, db) == 2
    con = connect(db)
    brands = locality.load_brands(con)
    con.close()
    assert locality.classify("Copper State Bolt & Nut", brands=brands)["value"] == "regional"
    assert locality.classify("Copper State Bolt & Nut Supply Co of Casa Grande", brands=brands)["value"] == "unknown"
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "cli.db"))
    assert main(["import-brands", "--file", str(path)]) == 0 and "2 brands stored" in capsys.readouterr().out


def test_overture_brands_are_stored(tmp_path):
    def fetch(release, area):
        return [{"name": "Subway", "street": "1 Main St", "city": "Casa Grande", "zip": "85122", "phone": "",
                 "website": "", "category": "restaurant", "brand": "Subway", "wikidata": "Q244457"},
                {"name": "Joe's Diner", "street": "2 Main St", "city": "Casa Grande", "zip": "85122", "phone": "",
                 "website": "", "category": "restaurant", "brand": "", "wikidata": ""}]

    db = tmp_path / "donkey.db"
    overture.snapshot_release("2026-09-23.1", "stephanie", fetch, db_path=db, out_dir=tmp_path)
    con = connect(db)
    rows = [tuple(r) for r in con.execute("SELECT name, brand, wikidata, release FROM place_brands")]
    con.close()
    assert rows == [("Subway", "Subway", "Q244457", "2026-09-23.1")]


def test_who_decides_cli_and_weekly_dry_run(tmp_path, monkeypatch, capsys):
    db = _notices(tmp_path, "Walmart Supercenter", "Gudelia LLC")
    monkeypatch.setenv("DONKEY_DB", str(db))
    assert main(["who-decides", "--no-npi"]) == 0
    out = capsys.readouterr().out
    assert "corporate" in out and "Walmart Supercenter" in out and "Gudelia LLC" in out
    assert main(["call-this-week", "--area", "stephanie"]) == 0
    assert "[who decides: corporate]" in capsys.readouterr().out
    monkeypatch.setenv("DONKEY_HOME", str(tmp_path))
    monkeypatch.setenv("DONKEY_AGENT_ID", "agent_fake")
    monkeypatch.setenv("DONKEY_ENVIRONMENT_ID", "env_fake")
    monkeypatch.setattr("donkey.runner.run_weekly", lambda *a, **k: {"status": "nothing_to_check", "town": "x"})
    monkeypatch.setattr("donkey.runner.run_notices", lambda *a, **k: {"status": "nothing_to_check", "town": "y"})
    assert main(["growth-weekly", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "board minutes: nothing for the paid check" in out and "who decides" in out and "Gudelia LLC" in out
