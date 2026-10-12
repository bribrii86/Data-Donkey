"""v0.5.5: weekly Pinal formation notices job with a fake Console client. No network, no paid calls."""
from datetime import date, datetime, timedelta, timezone

from donkey import api, ingest, runner
from donkey.ingest import connect, main
from donkey.triggers import looks_like_holding, looks_like_kiosk

from test_runner import FakeClient, _settings

TODAY = datetime.now(timezone.utc).date()
EFFECTIVE = (TODAY - timedelta(days=20)).isoformat()
HEADER = b"name,street,city,zip,address_type,agent,entity_id,entity_type,effective_date,newspaper,source_url,date_seen\n"
NOTICES = HEADER + (
    f"Gudelia LLC,820 W Cottonwood Ln Ste 5,Casa Grande,85122,agent,Rosique LLC,25122269,LLC,{EFFECTIVE},"
    f"Casa Grande Dispatch,https://example.test/n1,{TODAY}\n"
    f"408 N. Sacaton Street LLC,221 N Florence St,Casa Grande,85122,agent,Stephen R. Cooper,25125800,LLC,{EFFECTIVE},"
    f"Casa Grande Dispatch,https://example.test/n2,{TODAY}\n"
    f"Pathmaker Strategies LLC,901 N Colorado St,Casa Grande,85122,agent,Sheila Chavez,25124991,LLC,,"
    f"Casa Grande Dispatch,https://example.test/n3,{TODAY}\n"
    f"Gold Canyon Tile LLC,1 Kings Ranch Rd,Gold Canyon,85118,business,Jo Smith,25120001,LLC,{EFFECTIVE},"
    f"Arizona Business Gazette,https://example.test/n4,{TODAY}\n"
).encode()


def _notices(tmp_path, client, **kwargs):
    return runner.run_notices(_settings(tmp_path), client, db_path=tmp_path / "donkey.db", sleep=lambda s: None,
                              **kwargs)


def test_notices_become_new_llc_events_once_and_reach_call_this_week(tmp_path):
    client = FakeClient({"pinal-notices-01.csv": NOTICES}, cost="95")
    result = _notices(tmp_path, client)
    assert result["status"] == "done" and result["rows"] == 4 and result["cost_cents"] == 95
    create, message = client.calls[0], client.calls[1][1]
    assert create[3] == 150 and "formation notices" in create[4]  # own $1.50 cap
    assert "Pinal County" in message and "pinal-notices-NN.csv" in message and "arizonapublicnotices.com" in message

    db = tmp_path / "donkey.db"
    con = connect(db)
    events = {r["town"]: r["event_date"] for r in con.execute("SELECT * FROM events WHERE kind = 'new_llc'")}
    run = con.execute("SELECT town, status, rows, cost_cents FROM agent_runs").fetchone()
    con.close()
    assert len(events) == 2 and tuple(run) == ("Pinal notices", "done", 4, 95)
    hits = api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=db)
    names = sorted(h["name"] for h in hits)
    # holding LLC named after its address doesn't score; Gold Canyon is outside Stephanie's territory;
    # a blank effective date falls back to the day it was seen
    assert names == ["Gudelia LLC", "Pathmaker Strategies LLC"]
    # both notices give only the statutory agent's address, so the reason says to look the business up first
    assert {h["signals"][0] for h in hits} == {"new_llc_agent"}
    assert all("statutory agent's" in h["reasons"][0] and h["score"] == 3 for h in hits)

    again = _notices(tmp_path, FakeClient({"pinal-notices-01.csv": NOTICES}, cost="80"))
    assert again["rows"] == 0 and again["ingest"] is None  # same notices next week are not stored twice
    con = connect(db)
    assert con.execute("SELECT COUNT(*) FROM events WHERE kind = 'new_llc'").fetchone()[0] == 4
    assert runner.month_spend(con) == 175
    con.close()


def test_old_notices_drop_off_and_rotation_ignores_notices_runs(tmp_path):
    _notices(tmp_path, FakeClient({"p.csv": NOTICES}))
    later = (TODAY + timedelta(days=61)).isoformat()  # 60-day window, counted from the formation date
    assert api.call_this_week("stephanie", as_of=later, db_path=tmp_path / "donkey.db") == []
    con = connect(tmp_path / "donkey.db")
    assert runner.pick_town(con, "stephanie") == "Maricopa"  # first town, as if nothing had run
    con.close()


def test_holding_names():
    assert looks_like_holding("708 N. Beeline Hwy LLC") and looks_like_holding("200 W. Second Street LLC")
    assert looks_like_holding("Desert Sun Holdings LLC")
    assert looks_like_holding("1234 E Florence Blvd") and looks_like_holding("12 Main St., L.L.C.")
    assert not looks_like_holding("1st Choice Dental LLC") and not looks_like_holding("Gudelia, LLC")
    assert not looks_like_holding("2 Brothers Way Trucking LLC")  # street word mid-name is not an address
    assert not looks_like_holding("3 Rivers Lane Landscaping LLC")


def test_town_run_failure_still_runs_notices(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    monkeypatch.setenv("DONKEY_HOME", str(tmp_path))

    def broken(*args, **kwargs):
        raise RuntimeError("town run broke")

    calls = []
    monkeypatch.setattr(runner, "run_weekly", broken)
    monkeypatch.setattr(runner, "run_notices", lambda settings, dry_run=False: calls.append(dry_run) or {
        "status": "skipped_cap", "town": "Pinal notices", "month_spent_cents": 2500})
    monkeypatch.setattr("donkey.overture.snapshot_new", lambda area: [])
    assert main(["growth-weekly"]) == 1
    assert calls == [False]
    captured = capsys.readouterr()
    assert "town run broke" in captured.err and "Skipped Pinal notices" in captured.out
    assert main(["growth-weekly", "--notices-only", "--dry-run"]) == 0 and calls == [False, True]


def test_source_map_refuses_file_without_dates(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_bytes(b"name,city\nSomething LLC,Eloy\n")
    try:
        ingest.ingest("pinal-notices", path, "test", tmp_path / "donkey.db")
    except ValueError as exc:
        assert "event_date" in str(exc)
    else:
        raise AssertionError("expected a refusal")


def test_business_address_notice_scores_as_plain_new_llc(tmp_path):
    rows = HEADER + (f"Eloy Copy Center LLC,100 Main St,Eloy,85131,business,Jo Smith,25120002,LLC,{EFFECTIVE},"
                     f"Casa Grande Dispatch,https://example.test/n5,{TODAY}\n").encode()
    _notices(tmp_path, FakeClient({"p.csv": rows}))
    (hit,) = api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=tmp_path / "donkey.db")
    assert hit["signals"] == ["new_llc"] and hit["reasons"] == ["new business formed (formation notice)"]


def test_notices_cap_under_one_dollar_still_runs(tmp_path, monkeypatch):
    settings = {**_settings(tmp_path), "notices_cap_cents": 50}
    client = FakeClient({"p.csv": NOTICES}, cost="40")
    result = runner.run_notices(settings, client, db_path=tmp_path / "donkey.db", sleep=lambda s: None)
    assert result["status"] == "done" and client.calls[0][3] == 50


def test_any_job_error_still_runs_the_other(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    monkeypatch.setenv("DONKEY_HOME", str(tmp_path))
    calls = []

    def broken(*args, **kwargs):
        raise KeyError("id")  # e.g. an unexpected API response shape

    monkeypatch.setattr(runner, "run_weekly", broken)
    monkeypatch.setattr(runner, "run_notices", lambda settings, dry_run=False: calls.append(dry_run) or {
        "status": "skipped_cap", "town": "Pinal notices", "month_spent_cents": 2500})
    monkeypatch.setattr("donkey.overture.snapshot_new", lambda area: [])
    assert main(["growth-weekly"]) == 1 and calls == [False]
    assert "growth-weekly failed" in capsys.readouterr().err
    assert date.fromisoformat(EFFECTIVE)


def test_kiosk_filter_only_drops_overture_growth_rows(tmp_path):
    """v0.5.8 review: a formation notice is a real business even when its name reads like a kiosk."""
    notices = HEADER + (f"Bitcoin ATM,820 W Cottonwood Ln,Casa Grande,85122,business,Jo Smith,25129999,LLC,"
                        f"{EFFECTIVE},Casa Grande Dispatch,https://example.test/n9,{TODAY}\n").encode()
    _notices(tmp_path, FakeClient({"p.csv": notices}))
    hits = api.call_this_week("stephanie", as_of=TODAY.isoformat(), db_path=tmp_path / "donkey.db")
    assert [h["name"] for h in hits] == ["Bitcoin ATM"]


def test_kiosk_names():
    for name in ("Coinme", "Redbox", "ecoATM", "Chase ATM", "ATM Circle K", "Amazon Locker - Greek", "KeyMe Locksmiths",
                 "Bitcoin Depot | Bitcoin ATM", "CoinFlip Bitcoin ATM - JC Mart (Casa Grande)"):
        assert looks_like_kiosk(name), name
    for name in ("ATM Repair & Service", "Pinal ATM Services LLC", "Bitcoin Consulting LLC", "Budget Coin & Jewelry",
                 "Redbox Marketing Group", "Primo Water Dealer Office", "Glacier Water Services Inc",
                 "Atmosphere Salon", "Pop-A-Lock Locksmith", "Pinal County Planning & Development"):
        assert not looks_like_kiosk(name), name
