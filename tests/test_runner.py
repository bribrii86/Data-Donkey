"""v0.5.3: weekly growth runner with a fake Console client. No network, no paid calls."""
import sqlite3
from datetime import datetime, timezone

import pytest

from donkey import api, runner
from donkey.ingest import connect, main

PAGE1 = (b"name,street,city,zip,phone,website,category,home_based,source_url,date_seen\n"
         b"Desert Rose Plumbing,123 W Sunny Ln,Maricopa,85138,520-555-0101,,Plumber,yes,https://example.test/1,"
         b"2026-10-03\n")
PAGE1_LATER = PAGE1.replace(b"123 W Sunny Ln", b"450 N John Wayne Pkwy Ste 110").replace(b"yes,", b"no,")
PAGE2 = (b"Name,Street,City,ZIP,Phone,Website,Category,Home_Based\n"
         b"Copper Sky Dental,9 W Main St Ste 2,Maricopa,85138,520-555-0199,https://dental.test,Dentist,no\n"
         b"Out Of Town LLC,1 Elsewhere Rd,Phoenix,85004,602-555-0100,,Signs,no\n")


class FakeClient:
    def __init__(self, pages, cost="312", stops=("end_turn",)):
        self.pages, self.cost, self.stops = pages, cost, list(stops)
        self.calls, self.sent = [], []

    def create_session(self, agent_id, environment_id, budget_cents, title):
        self.calls.append(("create", agent_id, environment_id, budget_cents, title))
        return {"id": "sesn_fake"}

    def send_message(self, session_id, text):
        self.calls.append(("message", text))

    def get_session(self, session_id):
        return {"status": "idle", "usage": {"list_cost": {"amount": self.cost, "currency": "USD"}}}

    def list_events(self, session_id):
        stop = self.stops[0]
        if stop == "requires_action":
            return [{"id": "sevt_tool", "type": "agent.tool_use"}, {"id": "sevt_custom", "type": "agent.custom_tool_use"},
                    {"type": "session.status_idle",
                     "stop_reason": {"type": "requires_action", "event_ids": ["sevt_tool", "sevt_custom"]}}]
        return [{"type": "session.status_idle", "stop_reason": {"type": stop}}]

    def send_events(self, session_id, events):
        self.sent.append(events)
        if self.stops[0] == "requires_action":
            self.stops.pop(0)

    def list_files(self, session_id):
        return [{"id": f"file_{n}", "filename": name} for n, name in enumerate(self.pages)] + [
            {"id": "file_notes", "filename": "notes.txt"}]

    def download(self, file_id):
        if file_id == "file_notes":
            raise AssertionError("only CSV files are downloaded")
        return list(self.pages.values())[int(file_id.split("_")[1])]


def _settings(tmp_path, **overrides):
    env = {"ANTHROPIC_API_KEY": "test-key-not-real", "DONKEY_AGENT_ID": "agent_fake", "DONKEY_ENVIRONMENT_ID": "env_fake",
           **overrides}
    return runner.load_settings(env, home=tmp_path)


def _run(tmp_path, client, **kwargs):
    return runner.run_weekly(_settings(tmp_path), client, db_path=tmp_path / "donkey.db", sleep=lambda s: None,
                             **kwargs)


def test_settings_from_env_file(tmp_path):
    (tmp_path / ".env").write_text("# comment\nANTHROPIC_API_KEY='from-file'\nexport DONKEY_AGENT_ID=agent_x\n"
                                   "DONKEY_MONTHLY_CAP_USD=25\nDONKEY_RUN_CAP_USD=4.50\n", encoding="utf8")
    settings = runner.load_settings({"DONKEY_AGENT_ID": "agent_env"}, home=tmp_path)
    assert settings["api_key"] == "from-file" and settings["agent_id"] == "agent_env"  # environment wins
    assert settings["monthly_cap_cents"] == 2500 and settings["run_cap_cents"] == 450
    assert runner.load_settings({}, home=tmp_path / "empty")["monthly_cap_cents"] == 4000  # v0.6.0 default
    assert settings["area"] == "stephanie"
    with pytest.raises(ValueError, match="dollar amount"):
        runner.load_settings({"DONKEY_MONTHLY_CAP_USD": "lots"}, home=tmp_path)
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
        runner.ConsoleClient("")


def test_two_weekly_runs_snapshot_diff_and_reach_call_this_week(tmp_path):
    first = FakeClient({"maricopa-page-01.csv": PAGE1, "maricopa-page-02.csv": PAGE2})
    result = _run(tmp_path, first, town="Maricopa")
    assert result["status"] == "done" and result["cost_cents"] == 312
    assert result["files"] == 2 and result["rows"] == 3
    assert result["snapshot"]["row_count"] == 2  # Phoenix row dropped by the stephanie area filter
    assert result["diff"]["baseline"] is True
    create = first.calls[0]
    assert create[:4] == ("create", "agent_fake", "env_fake", 600) and "Maricopa" in create[4]
    prompt = first.calls[1][1]
    assert "Maricopa, Arizona" in prompt and "maricopa-page-NN.csv" in prompt and "home_based" in prompt

    con = connect(tmp_path / "donkey.db")
    con.execute("UPDATE snapshots SET taken_at = '2026-10-03'")  # pretend the first run was last week
    con.commit()
    con.close()
    second = FakeClient({"maricopa-page-01.csv": PAGE1_LATER, "maricopa-page-02.csv": PAGE2}, cost="288")
    result = _run(tmp_path, second, town="Maricopa")
    assert result["diff"]["by_kind"] == {"growth_home_to_office": 1}
    today = datetime.now(timezone.utc).date().isoformat()
    hits = api.call_this_week("stephanie", as_of=today, db_path=tmp_path / "donkey.db")
    assert [(h["name"], h["signals"]) for h in hits] == [("Desert Rose Plumbing", ["home_to_office"])]

    con = connect(tmp_path / "donkey.db")
    runs = con.execute("SELECT status, cost_cents, rows, changes FROM agent_runs ORDER BY id").fetchall()
    assert [tuple(r) for r in runs] == [("done", 312, 3, 0), ("done", 288, 3, 1)]
    assert runner.month_spend(con) == 600
    con.close()


def test_monthly_cap_limits_budget_then_skips(tmp_path):
    db = tmp_path / "donkey.db"
    con = connect(db)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    con.execute("INSERT INTO agent_runs (started_at, area, town, session_id, status, budget_cents, cost_cents) "
                f"VALUES ('{month}-01T03:00:00+00:00', 'stephanie', 'Eloy', 's1', 'done', 600, 2100)")
    con.execute("INSERT INTO agent_runs (started_at, area, town, session_id, status, budget_cents) "
                f"VALUES ('{month}-02T03:00:00+00:00', 'stephanie', 'Coolidge', 's2', 'running', 250)")  # crashed
    con.execute("INSERT INTO agent_runs (started_at, area, town, session_id, status, budget_cents, cost_cents) "
                "VALUES ('2000-01-01T03:00:00+00:00', 'stephanie', 'Florence', 's0', 'done', 600, 9999)")  # old
    con.commit()
    assert runner.month_spend(con) == 2350
    con.close()

    capped = _settings(tmp_path, DONKEY_MONTHLY_CAP_USD="25")
    client = FakeClient({"x.csv": PAGE2}, cost="140")
    result = runner.run_weekly(capped, client, db_path=db, sleep=lambda s: None, town="Maricopa")
    assert result["budget_cents"] == 150  # $25 - $23.50 left
    assert client.calls[0][3] == 150

    skipped = FakeClient({})
    result = runner.run_weekly(capped, skipped, db_path=db, sleep=lambda s: None, town="Maricopa")
    assert result["status"] == "skipped_cap" and skipped.calls == []  # no API call at all
    con = connect(db)
    assert con.execute("SELECT status FROM agent_runs ORDER BY id DESC").fetchone()[0] == "skipped_cap"
    con.close()


def test_rotation_paused_tools_denied_and_budget_reached(tmp_path):
    db = tmp_path / "donkey.db"
    con = connect(db)
    assert runner.pick_town(con, "stephanie") == "Maricopa"  # first town in areas.json, never run
    con.close()
    client = FakeClient({"m.csv": PAGE2}, stops=("requires_action", "budget_reached"))
    result = _run(tmp_path, client)
    assert result["town"] == "Maricopa" and result["status"] == "budget_reached"
    assert client.sent[0] == [
        {"type": "user.tool_confirmation", "tool_use_id": "sevt_tool", "result": "deny"},
        {"type": "user.custom_tool_result", "custom_tool_use_id": "sevt_custom",
         "content": [{"type": "text", "text": "Not available in the scheduled run."}]}]
    con = connect(db)
    assert runner.pick_town(con, "stephanie") != "Maricopa"  # rotates on
    con.close()


def test_errors_are_logged_and_dry_run_makes_no_calls(tmp_path):
    class Broken(FakeClient):
        def send_message(self, session_id, text):
            raise RuntimeError("POST /v1/sessions/sesn_fake/events failed: HTTP 500")

    with pytest.raises(RuntimeError):
        _run(tmp_path, Broken({}), town="Eloy")
    con = sqlite3.connect(tmp_path / "donkey.db")
    status, note = con.execute("SELECT status, note FROM agent_runs").fetchone()
    con.close()
    assert status == "error" and "HTTP 500" in note

    class NoSession(FakeClient):
        def create_session(self, *args):
            raise RuntimeError("POST /v1/sessions failed: HTTP 400")

    with pytest.raises(RuntimeError):
        _run(tmp_path, NoSession({}), town="Eloy")
    con = connect(tmp_path / "donkey.db")
    cost = con.execute("SELECT cost_cents FROM agent_runs ORDER BY id DESC").fetchone()[0]
    assert cost == 0 and runner.month_spend(con) == 600  # failed create counts $0; the session run counts its budget
    con.close()

    quiet = FakeClient({})
    result = _run(tmp_path, quiet, town="Eloy", dry_run=True)
    assert result["status"] == "dry_run" and "Eloy, Arizona" in result["prompt"] and quiet.calls == []


def test_timeout_interrupts(tmp_path):
    class Busy(FakeClient):
        def get_session(self, session_id):
            return {"status": "running"} if len(self.sent) == 0 else super().get_session(session_id)

    settings = _settings(tmp_path, DONKEY_RUN_TIMEOUT_MINUTES="1")
    client = Busy({})
    result = runner.run_weekly(settings, client, db_path=tmp_path / "donkey.db", poll_seconds=30, sleep=lambda s: None)
    assert result["status"] == "timeout" and client.sent == [[{"type": "user.interrupt"}]] and result["rows"] == 0


def test_task_command_and_cli(tmp_path, monkeypatch, capsys):
    command = runner.task_command("sun", "03:00", python="C:\\Py\\python.exe")
    assert command[:3] == ["schtasks", "/Create", "/F"] and command[-1] == '"C:\\Py\\python.exe" -m donkey growth-weekly'
    with pytest.raises(ValueError):
        runner.task_command("SUNDAY")
    with pytest.raises(ValueError):
        runner.task_command("SUN", "25:00")
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "donkey.db"))
    monkeypatch.setenv("DONKEY_HOME", str(tmp_path))
    assert main(["growth-weekly", "--dry-run", "--town", "Eloy"]) == 0
    out = capsys.readouterr().out
    assert "job: Eloy (stephanie)" in out and "this run's cap: $6.00" in out
    assert "job: Pinal notices (stephanie)" in out and "this run's cap: $1.50" in out
    assert main(["growth-weekly", "--install-task", "--dry-run", "--day", "MON"]) == 0
    assert "Would run: schtasks /Create /F /SC WEEKLY /D MON" in capsys.readouterr().out
    monkeypatch.setenv("DONKEY_RUN_AREA", "nowhere")
    assert main(["growth-weekly", "--dry-run"]) == 1


def test_cap_check_and_claim_are_one_transaction(tmp_path, monkeypatch):
    """While another runner is between its cap check and its claim, a second run waits and makes no API call."""
    db = tmp_path / "donkey.db"
    connect(db).close()

    def quick_connect(path=None):
        con = connect(path)
        con.execute("PRAGMA busy_timeout = 100")
        return con

    monkeypatch.setattr(runner, "connect", quick_connect)
    other = sqlite3.connect(db, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # the first run holds the write lock
    client = FakeClient({})
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        _run(tmp_path, client, town="Eloy")
    assert client.calls == []
    other.execute("ROLLBACK")
    other.close()
    assert _run(tmp_path, client, town="Eloy")["status"] == "done"
