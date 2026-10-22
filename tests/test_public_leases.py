"""v0.6.9: public-body copier lease calendar (fake Console client, made-up bodies and filings; no network)."""
import json
import zipfile

from donkey import public_leases, runner
from donkey.ingest import connect, ingest, main
from test_runner import FakeClient

BODIES = ("body_name,body_type,zip_or_area,agendas_url,minutes_url\n"
          "Coolidge Unified School District #21,school_district,85128,https://cusd.test/a,https://cusd.test/m\n"
          "City of Coolidge,city,85128,https://coolidge.test/a,\n"
          "Toltec School District #22,school_district,85131,https://toltec.test/a,https://toltec.test/m\n")
UCC = ("Debtor,Filing Date,File Number,Secured Party\n"
       "COOLIDGE UNIFIED SCHOOL DISTRICT NO 21,07/20/2025,202500001,U.S. Bank Equipment Finance\n"
       "COOLIDGE UNIFIED SCHOOL DISTRICT NO 3,01/05/2024,202400009,Other Bank\n")
FOUND = ("body_name,status,dealer,lessor,make,units,term_months,monthly_amount,annual_amount,contract_vehicle,"
         "approved_date,next_opening,source_url,notes\n"
         "Coolidge Unified School District #21,lease_found,Imagine Technology,U.S. Bank,Sharp,40,60,\"$13,387.91\",,"
         "Sourcewell,07/09/2025,2030-07,https://cusd.test/m/2025-07-09.pdf,District-wide lease.\n"
         "City of Coolidge,nothing_public,,,,,,,,,,,,No copier items.\n"
         "Toltec School District #22,lead,,,Kyocera,9,36,1759.65,,AEPA,2021-09-27,2027-09,https://toltec.test/x,\n"
         "Somewhere Else USD,lease_found,X,,,,,,,,,2026-01,,\n")


def _settings(tmp_path, **env):
    return runner.load_settings({"ANTHROPIC_API_KEY": "test-key-not-real", "DONKEY_AGENT_ID": "agent_fake",
                                 "DONKEY_ENVIRONMENT_ID": "env_fake", **env}, home=tmp_path)


def _setup(tmp_path):
    (tmp_path / "bodies.csv").write_text(BODIES, encoding="utf8")
    ucc = tmp_path / "ucc.csv"
    ucc.write_text(UCC, encoding="utf8")
    db = tmp_path / "donkey.db"
    ingest("az-sos-ucc", ucc, "test", db)
    return db


def test_dry_run_shows_ucc_months_and_spends_nothing(tmp_path):
    db = _setup(tmp_path)
    client = FakeClient({})
    result = public_leases.run(_settings(tmp_path), "Pinal County", client=client, dry_run=True, db_path=db,
                               sleep=lambda s: None)
    assert client.calls == [] and result["bodies"] == 3 and result["with_ucc_months"] == 1
    prompt = result["sessions"][0]["prompt"]
    assert "2025-07 (U.S. Bank Equipment Finance)" in prompt and "Other Bank" not in prompt  # district number differs
    assert "Pinal County, AZ" in prompt
    city_line = next(line for line in prompt.splitlines() if line.startswith("City of Coolidge"))
    assert "U.S. Bank" not in city_line  # a city's row never gets its school district's filings


def test_run_stores_rows_then_calendar_and_skip(tmp_path):
    db = _setup(tmp_path)
    client = FakeClient({"public-leases-01.csv": FOUND.encode()}, cost="900")
    result = public_leases.run(_settings(tmp_path), "Pinal", client=client, db_path=db, sleep=lambda s: None)
    assert result["rows"] == 3 and result["cost_cents"] == 900 and result["sessions"][0]["dropped"] == 1
    assert client.calls[0][3] == 2500  # whole-run cap $25 by default
    rows = public_leases.calendar("Pinal", db_path=db)
    assert [r["body_name"] for r in rows] == ["Toltec School District #22", "Coolidge Unified School District #21"]
    coolidge = rows[1]
    assert coolidge["monthly_amount"] == "13387.91" and coolidge["approved_date"] == "2025-07-09"
    assert coolidge["zip"] == "85128" and coolidge["county"] == "Pinal" and coolidge["status"] == "lease_found"
    assert len(public_leases.calendar("Pinal", db_path=db, include_empty=True)) == 3
    assert public_leases.calendar("Maricopa", db_path=db) == []

    again = public_leases.run(_settings(tmp_path), "Pinal", client=FakeClient({}), dry_run=True, db_path=db,
                              sleep=lambda s: None)
    assert again["skipped_recent"] == 2  # the city had nothing public, so it is read again
    assert "City of Coolidge" in again["sessions"][0]["prompt"]
    assert "Toltec School District #22,school" not in again["sessions"][0]["prompt"]


def test_batches_split_the_cap(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    monkeypatch.setattr(public_leases, "BATCH", 2)
    client = FakeClient({"public-leases-01.csv": FOUND.encode()}, cost="300")
    result = public_leases.run(_settings(tmp_path, DONKEY_PUBLIC_LEASES_CAP_USD="10"), "Pinal", client=client,
                               db_path=db, sleep=lambda s: None)
    budgets = [call[3] for call in client.calls if call[0] == "create"]
    assert budgets == [500, 700]  # second batch gets what the first left
    assert len(result["sessions"]) == 2 and result["cost_cents"] == 600


def test_import_hand_built_csv_and_export(tmp_path):
    db = tmp_path / "donkey.db"
    hand = tmp_path / "public_body_copier_leases.csv"
    hand.write_text("Body,Finding,Dealer,Lender,Brand,Next opening,Source,Angle\n"
                    "Central Arizona College,Full lease found,Fruth Group,Canon Financial,Canon,about 2027-28,"
                    "https://cac.test/m.pdf,Get on the bid list.\n"
                    "Eloy ESD #11,Nothing public,,,,,,Call the business office.\n"
                    "City of Eloy,Current vendor known,,,Sharp,,,\n", encoding="utf8")
    done = public_leases.import_file(hand, "Pinal", db_path=db, home=tmp_path)
    assert done["rows"] == 3 and done["ingest"]["row_count"] == 3
    rows = public_leases.calendar(db_path=db)
    assert [(r["body_name"], r["status"]) for r in rows] == [("Central Arizona College", "lease_found"),
                                                             ("City of Eloy", "vendor_known")]
    csv_path, xlsx_path = public_leases.write_calendar(rows, tmp_path / "out" / "pinal-leases")
    assert csv_path.read_text(encoding="utf8").startswith("next_opening,body_name")
    with zipfile.ZipFile(xlsx_path) as z:
        assert "Fruth Group" in z.read("xl/worksheets/sheet1.xml").decode()
    con = connect(db)
    details = json.loads(con.execute("SELECT details_json FROM events WHERE kind = 'public_lease'").fetchone()[0])
    con.close()
    assert details["county"] == "Pinal" and details["state"] == "AZ"


def test_status_words_and_opening_order():
    assert public_leases.status("Full lease found") == "lease_found"
    assert public_leases.status("Strong lead") == "lead"
    assert public_leases.status("Blocked (site gone)") == "blocked"
    assert public_leases.status("") == ""
    keys = sorted(["", "about fall 2027", "2027-03", "2026-11-11"], key=public_leases._opening)
    assert keys == ["2026-11-11", "2027-03", "about fall 2027", ""]


def test_cli_bodies_prompt_and_calendar(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    assert main(["bodies-prompt", "--county", "Maricopa"]) == 0
    assert "Maricopa County, AZ" in capsys.readouterr().out
    connect().close()
    assert main(["public-lease-calendar", "--county", "Pinal"]) == 0
    assert "0 public bodies" in capsys.readouterr().out
