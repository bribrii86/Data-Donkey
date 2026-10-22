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
    toltec_line = next(line for line in prompt.splitlines() if line.startswith("Toltec"))
    assert toltec_line.endswith(",")  # no filings named Toltec
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
    assert again["skipped_recent"] == 3 and again["status"] == "nothing_to_do"  # empty bodies wait RETRY_DAYS


def test_empty_bodies_retry_after_90_days_and_leases_after_a_year(tmp_path, monkeypatch):
    db = _setup(tmp_path)
    public_leases.run(_settings(tmp_path), "Pinal", client=FakeClient({"p-01.csv": FOUND.encode()}), db_path=db,
                      sleep=lambda s: None)
    con = connect(db)
    con.execute("UPDATE events SET event_date = '2020-01-01' WHERE kind = 'public_lease' AND details_json LIKE "
                "'%nothing_public%'")
    con.commit()
    con.close()
    again = public_leases.run(_settings(tmp_path), "Pinal", client=FakeClient({}), dry_run=True, db_path=db,
                              sleep=lambda s: None)
    assert again["skipped_recent"] == 2 and "City of Coolidge" in again["sessions"][0]["prompt"]
    assert "Toltec School District #22,school" not in again["sessions"][0]["prompt"]


def test_better_finding_wins_across_pages_and_over_a_newer_empty_row(tmp_path):
    db = _setup(tmp_path)
    later = ("body_name,status,notes\nCoolidge Unified School District #21,blocked,Site down.\n"
             "Toltec School District #22,not found,\n")
    client = FakeClient({"public-leases-01.csv": FOUND.encode(), "public-leases-02.csv": later.encode()})
    public_leases.run(_settings(tmp_path), "Pinal", client=client, db_path=db, sleep=lambda s: None)
    assert {r["body_name"]: r["status"] for r in public_leases.calendar("Pinal", db_path=db)} == {
        "Coolidge Unified School District #21": "lease_found", "Toltec School District #22": "lead"}
    hand = tmp_path / "recheck.csv"
    hand.write_text("Body,Status\nCoolidge Unified School District #21,Nothing found\n", encoding="utf8")
    public_leases.import_file(hand, "Pinal", db_path=db, home=tmp_path, checked="2030-01-01")
    rows = public_leases.calendar("Pinal", db_path=db)
    assert next(r for r in rows if r["body_name"].startswith("Coolidge"))["dealer"] == "Imagine Technology"


def test_import_without_status_column_and_formula_cells(tmp_path):
    db = tmp_path / "donkey.db"
    hand = tmp_path / "plain.csv"
    hand.write_text("Body,Dealer,Term,Make,Next,Notes,Source\n"
                    "Eloy City,Sharp Dealer,60,Sharp,2028-01,=HYPERLINK(\"http://x.test\"),javascript:alert(1)\n"
                    "Picacho SD,,,,,,\n,Orphan Dealer,,,,,\n", encoding="utf8")
    done = public_leases.import_file(hand, "Pinal", db_path=db, home=tmp_path)
    assert done["rows"] == 2 and done["skipped_no_name"] == 1
    rows = public_leases.calendar(db_path=db, include_empty=True)
    assert {r["body_name"]: r["status"] for r in rows} == {"Eloy City": "lease_found", "Picacho SD": "nothing_public"}
    assert rows[0]["source_url"] == ""  # only http(s) links are kept
    csv_path, _ = public_leases.write_calendar(rows, tmp_path / "cal")
    text = csv_path.read_text(encoding="utf8")
    assert "'=HYPERLINK" in text and ",'," not in text and ",'\n" not in text  # blank cells stay blank


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
    assert public_leases.status("not found") == public_leases.status("No lease found") == "nothing_public"
    assert public_leases.status("Lease found (no term)") == "lease_found"
    assert public_leases._opening("maybe 2027", 2026) == public_leases._opening("decision 2027", 2026) == "2027-13"
    assert public_leases._opening("September 2027", 2026) == public_leases._opening("Sept 2027", 2026) == "2027-09"
    # Buck's hand-built notes: past years are history, the earliest future year is the opening
    assert public_leases._opening("unknown; no council item since 2013", 2026) == "9999"
    assert public_leases._opening("renewed ~fall 2024 with no board item; next about fall 2027", 2026) == "2027-13"
    assert public_leases._opening("~Sep 2030", 2026) == "2030-09"
    assert public_leases._opening("2027-2028 (est.)", 2026) == "2027-13"
    assert public_leases._opening("IM7000s ~3 years old in 2027", 2026) == "2027-13"
    assert public_leases._opening("bid 20300 units", 2026) == "9999"
    keys = sorted(["", "about fall 2027", "2027-03", "2026-11-11", "March 2027", "2027-12"], key=lambda v: public_leases._opening(v, 2026))
    assert keys == ["2026-11-11", "2027-03", "March 2027", "2027-12", "about fall 2027", ""]


def test_cli_bodies_prompt_and_calendar(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    assert main(["bodies-prompt", "--county", "Maricopa"]) == 0
    assert "Maricopa County, AZ" in capsys.readouterr().out
    connect().close()
    assert main(["public-lease-calendar", "--county", "Pinal"]) == 0
    assert "0 public bodies" in capsys.readouterr().out


def test_import_buck_hand_built_headers(tmp_path):
    db = tmp_path / "donkey.db"
    hand = tmp_path / "public_body_copier_leases.csv"
    hand.write_text("body,town,found,dealer,lessor,equipment,term,amount,contract_vehicle,meeting_date,ucc_filing,"
                    "est_next_opening,source_url,note\n"
                    "Toltec SD #22,Toltec,strong lead,,,9 Kyocera,36 months,\"$1,759.65/mo\",AEPA,2021-09-27,,"
                    "2027-09,https://toltec.test/x,FMV lease.\n"
                    "Coolidge USD #21,Coolidge,full lease,Imagine Technology,U.S. Bank,Sharp,60,\"$803K total\","
                    "Sourcewell,2025-07-09,2025-07-20 U.S. Bank,2030-07,,\n", encoding="utf8")
    public_leases.import_file(hand, "Pinal", db_path=db, home=tmp_path)
    rows = {r["body_name"]: r for r in public_leases.calendar("Pinal", db_path=db)}
    assert rows["Toltec SD #22"]["next_opening"] == "2027-09" and rows["Toltec SD #22"]["monthly_amount"] == "1759.65"
    assert rows["Toltec SD #22"]["approved_date"] == "2021-09-27" and rows["Toltec SD #22"]["status"] == "lead"
    coolidge = rows["Coolidge USD #21"]
    assert coolidge["status"] == "lease_found" and coolidge["next_opening"] == "2030-07"
    assert "(amount: $803K total)" in coolidge["notes"] and "(UCC: 2025-07-20 U.S. Bank)" in coolidge["notes"]


def test_import_amount_only_when_unit_follows_a_plain_number(tmp_path):
    db = tmp_path / "donkey.db"
    hand = tmp_path / "amounts.csv"
    hand.write_text("body,found,dealer,amount\nA District,lead,X,\"$49,082.88 per year\"\n"
                    "B District,lead,X,\"$5,000 total over 36 months\"\nC District,lead,X,$803K/yr\n", encoding="utf8")
    public_leases.import_file(hand, "Pinal", db_path=db, home=tmp_path)
    rows = {r["body_name"]: r for r in public_leases.calendar("Pinal", db_path=db)}
    assert rows["A District"]["annual_amount"] == "49082.88" and rows["A District"]["monthly_amount"] == ""
    for name, text in (("B District", "$5,000 total over 36 months"), ("C District", "$803K/yr")):
        assert rows[name]["monthly_amount"] == rows[name]["annual_amount"] == "" and f"(amount: {text})" in rows[name]["notes"]
