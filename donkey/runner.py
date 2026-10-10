"""Weekly growth runner: ask Buck's Console research agent (Managed Agents) to list one town's businesses, save the
CSV pages it writes, then snapshot and diff them so growth events reach call-this-week.

Paid calls, so every run is capped twice: a monthly cap kept in donkey.db (agent_runs) and a hard per-session
budget the platform enforces. The API key comes from the environment or a gitignored .env, never from the repo.
Standard library only (urllib); every HTTP call is logged without the key."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import sqlite3
import subprocess  # nosec B404
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from . import areas, growth
from .common import DEFAULT_DB
from .ingest import connect

__all__ = ["ConsoleClient", "Settings", "load_settings", "pick_town", "run_weekly", "month_spend", "task_command"]

API = "https://api.anthropic.com"
BETA = "managed-agents-2026-04-01"
HOME = DEFAULT_DB.parent  # ~/DataDonkey: .env, runs/, logs/
COLUMNS = ("name", "street", "city", "zip", "phone", "website", "category", "home_based", "source_url", "date_seen")
MIN_RUN_CENTS = 100  # don't start a run with less than $1 left this month
TASK_NAME = "DataDonkey Growth Weekly"

PROMPT = """List every business you can find that is physically located in {town}, Arizona (all categories, including
home-based businesses and trades; do not skip home businesses). Use free and paid web directories and business listing
sites you can read.

Save results as you go: after each directory page, write that page's rows to /mnt/session/outputs/{slug}-page-NN.csv
(NN = 01, 02, ...) and do not carry earlier pages in memory. CSV columns, in this order:
{columns}
- home_based: yes if the business runs from a home address (no suite, residential street, or the listing says so),
  no if it has a storefront, office or suite, blank if unsure.
- date_seen: {today}. source_url: the page the row came from.
- One row per business; leave a field blank rather than guessing. Skip businesses outside {town}.
Stop when the directories run out. Reply with one line: pages saved and rows written."""


class Client(Protocol):
    def create_session(self, agent_id: str, environment_id: str, budget_cents: int, title: str) -> dict: ...
    def send_message(self, session_id: str, text: str) -> None: ...
    def get_session(self, session_id: str) -> dict: ...
    def list_events(self, session_id: str) -> list[dict]: ...
    def send_events(self, session_id: str, events: list[dict]) -> None: ...
    def list_files(self, session_id: str) -> list[dict]: ...
    def download(self, file_id: str) -> bytes: ...


class ConsoleClient:
    """Raw HTTP to the Managed Agents API (no SDK, so donkey stays standard-library only)."""

    def __init__(self, api_key: str, log_path: Path | None = None, base: str = API):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set (environment or ~/DataDonkey/.env)")
        self._key = api_key
        self._base = base
        self._log = log_path

    def _call(self, method: str, path: str, body: dict | None = None, raw: bool = False) -> Any:
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01", "anthropic-beta": BETA}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf8")
            headers["content-type"] = "application/json"
        request = urllib.request.Request(self._base + path, data=data, headers=headers, method=method)
        status, started = 0, time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # nosec B310
                status = response.status
                payload = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            detail = exc.read().decode("utf8", "replace")[:500]
            raise RuntimeError(f"{method} {path} failed: HTTP {exc.code} {detail}") from exc
        finally:
            self._write_log(method, path, status, time.monotonic() - started)
        return payload if raw else json.loads(payload or b"{}")

    def _write_log(self, method: str, path: str, status: int, seconds: float) -> None:
        if self._log is None:
            return
        self._log.parent.mkdir(parents=True, exist_ok=True)
        line = {"at": _now(), "method": method, "path": path.split("?")[0], "status": status,
                "seconds": round(seconds, 2)}
        with self._log.open("a", encoding="utf8") as stream:
            stream.write(json.dumps(line) + "\n")

    def create_session(self, agent_id: str, environment_id: str, budget_cents: int, title: str) -> dict:
        return self._call("POST", "/v1/sessions", {
            "agent": {"type": "agent", "id": agent_id}, "environment_id": environment_id, "title": title,
            "budget": {"type": "limit", "max_list_cost": {"amount": str(int(budget_cents)), "currency": "USD"}},
        })

    def send_message(self, session_id: str, text: str) -> None:
        self.send_events(session_id, [{"type": "user.message", "content": [{"type": "text", "text": text}]}])

    def send_events(self, session_id: str, events: list[dict]) -> None:
        self._call("POST", f"/v1/sessions/{session_id}/events", {"events": events})

    def get_session(self, session_id: str) -> dict:
        return self._call("GET", f"/v1/sessions/{session_id}")

    def list_events(self, session_id: str) -> list[dict]:
        events: list[dict] = []
        page = None
        while True:
            query = f"?page={urllib.parse.quote(page)}" if page else ""
            result = self._call("GET", f"/v1/sessions/{session_id}/events{query}")
            events.extend(result.get("data", []))
            page = result.get("next_page")
            if not page:
                return events

    def list_files(self, session_id: str) -> list[dict]:
        files: list[dict] = []
        after = ""
        while True:
            query = {"scope_id": session_id, "limit": "100", **({"after_id": after} if after else {})}
            result = self._call("GET", f"/v1/files?{urllib.parse.urlencode(query)}")
            files.extend(result.get("data", []))
            if not result.get("has_more") or not result.get("last_id"):
                return files
            after = result["last_id"]

    def download(self, file_id: str) -> bytes:
        return self._call("GET", f"/v1/files/{file_id}/content", raw=True)


class Settings(dict):
    """agent_id, environment_id, api_key, monthly_cap_cents, run_cap_cents, area, timeout_minutes, home."""


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text(encoding="utf8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip().removeprefix("export ").strip()] = value.strip().strip("\"'")
    return values


def _cents(value: str, name: str) -> int:
    try:
        cents = round(float(value) * 100)
    except ValueError:
        raise ValueError(f"{name} must be a dollar amount, got {value!r}") from None
    if cents <= 0:
        raise ValueError(f"{name} must be more than 0")
    return cents


def load_settings(env: dict[str, str] | None = None, home: Path | None = None) -> Settings:
    """Environment first, then the .env file (DONKEY_ENV_FILE, else ~/DataDonkey/.env)."""
    environ = dict(os.environ if env is None else env)
    home = Path(home or environ.get("DONKEY_HOME") or HOME)
    merged = {**_read_env_file(Path(environ.get("DONKEY_ENV_FILE") or home / ".env")), **environ}
    return Settings(
        api_key=merged.get("ANTHROPIC_API_KEY", ""),
        agent_id=merged.get("DONKEY_AGENT_ID", ""),
        environment_id=merged.get("DONKEY_ENVIRONMENT_ID", ""),
        monthly_cap_cents=_cents(merged.get("DONKEY_MONTHLY_CAP_USD", "25"), "DONKEY_MONTHLY_CAP_USD"),
        run_cap_cents=_cents(merged.get("DONKEY_RUN_CAP_USD", "6"), "DONKEY_RUN_CAP_USD"),
        area=merged.get("DONKEY_RUN_AREA", "stephanie"),
        timeout_minutes=int(merged.get("DONKEY_RUN_TIMEOUT_MINUTES", "90")),
        home=home,
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def month_spend(con: sqlite3.Connection, now: datetime | None = None) -> int:
    """Cents spent this calendar month (UTC). A run with no final cost (still running, or crashed) counts at its
    full budget, so a crash can never let the month go over the cap."""
    month = (now or datetime.now(timezone.utc)).strftime("%Y-%m")
    row = con.execute(
        "SELECT COALESCE(SUM(COALESCE(cost_cents, budget_cents, 0)), 0) AS spent FROM agent_runs "
        "WHERE substr(started_at, 1, 7) = ?", (month,)
    ).fetchone()
    return int(row["spent"])


def pick_town(con: sqlite3.Connection, area: str) -> str:
    """Next town in the area's rotation: never run first (in areas.json order), else the longest since a run."""
    towns = areas.load(area)["towns"]
    if not towns:
        raise ValueError(f"area {area!r} has no towns to rotate through; add cities in areas.json")
    last = {row["town"]: row["last"] for row in con.execute(
        "SELECT town, MAX(started_at) AS last FROM agent_runs WHERE area = ? AND session_id IS NOT NULL "
        "GROUP BY town", (area,))}
    return min(towns, key=lambda town: (town in last, last.get(town, ""), towns.index(town)))


def _slug(town: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", town.casefold()).strip("-")


def _stop_reason(events: list[dict]) -> dict:
    for event in reversed(events):
        if event.get("type") == "session.status_idle":
            return event.get("stop_reason") or {}
    return {}


def _settle(client: Client, session_id: str, stop: dict) -> None:
    """Nobody watches a scheduled run, so every paused tool call is denied and custom tools get 'not available'."""
    by_id = {event.get("id"): event for event in client.list_events(session_id)}
    replies = []
    for event_id in stop.get("event_ids", []):
        if by_id.get(event_id, {}).get("type") == "agent.custom_tool_use":
            replies.append({"type": "user.custom_tool_result", "custom_tool_use_id": event_id,
                            "content": [{"type": "text", "text": "Not available in the scheduled run."}]})
        else:
            replies.append({"type": "user.tool_confirmation", "tool_use_id": event_id, "result": "deny"})
    if replies:
        client.send_events(session_id, replies)


def _wait(client: Client, session_id: str, timeout_s: float, poll_s: float, sleep: Callable[[float], None]) -> str:
    """Poll until the agent is done. Returns done, budget_reached, terminated or timeout."""
    waited, settled = 0.0, 0
    while waited < timeout_s:
        sleep(poll_s)
        waited += poll_s
        status = client.get_session(session_id).get("status")
        if status == "terminated":
            return "terminated"
        if status != "idle":
            continue
        stop = _stop_reason(client.list_events(session_id))
        kind = stop.get("type")
        if kind == "budget_reached":
            return "budget_reached"
        if kind == "requires_action" and settled < 20:
            _settle(client, session_id, stop)
            settled += 1
            continue
        return "done"
    client.send_events(session_id, [{"type": "user.interrupt"}])
    return "timeout"


def _merge_csvs(blobs: list[tuple[str, bytes]], out: Path) -> int:
    rows: list[dict[str, str]] = []
    for _, blob in sorted(blobs):
        for row in csv.DictReader(io.StringIO(blob.decode("utf-8-sig", "replace"))):
            lower = {str(k).strip().casefold(): (v or "").strip() for k, v in row.items() if k}
            if lower.get("name"):
                rows.append({column: lower.get(column, "") for column in COLUMNS})
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def run_weekly(
    settings: Settings,
    client: Client | None = None,
    town: str | None = None,
    dry_run: bool = False,
    db_path: str | Path | None = None,
    poll_seconds: float = 30,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """One scheduled run. Returns what happened; every outcome, including a skip, is a row in agent_runs."""
    area = settings["area"]
    con = connect(db_path)
    try:
        # Read the month's spend and claim this run's budget in one write transaction, so two runs started together
        # can't both see the same money left. Closing without commit (dry run, missing settings) rolls it back.
        con.execute("BEGIN IMMEDIATE")
        town = town or pick_town(con, area)
        spent = month_spend(con)
        left = settings["monthly_cap_cents"] - spent
        budget = min(settings["run_cap_cents"], left)
        prompt = PROMPT.format(town=town, slug=_slug(town), columns=",".join(COLUMNS),
                               today=datetime.now(timezone.utc).date().isoformat())
        result: dict[str, Any] = {"town": town, "area": area, "month_spent_cents": spent, "budget_cents": budget}
        if dry_run:
            return {**result, "status": "dry_run", "prompt": prompt}
        if budget < MIN_RUN_CENTS:
            con.execute("INSERT INTO agent_runs (started_at, finished_at, area, town, status, budget_cents, cost_cents, "
                        "note) VALUES (?, ?, ?, ?, 'skipped_cap', 0, 0, ?)",
                        (_now(), _now(), area, town, f"monthly cap reached: {spent} of "
                                                     f"{settings['monthly_cap_cents']} cents"))
            con.commit()
            return {**result, "status": "skipped_cap"}
        for field in ("agent_id", "environment_id"):
            if not settings[field]:
                raise ValueError(f"DONKEY_{field.upper()} is not set (environment or ~/DataDonkey/.env)")
        if client is None:
            client = ConsoleClient(settings["api_key"], settings["home"] / "logs" / "console-calls.jsonl")
        run_id = int(con.execute(
            "INSERT INTO agent_runs (started_at, area, town, status, budget_cents) VALUES (?, ?, ?, 'starting', ?)",
            (_now(), area, town, budget)).lastrowid)
        con.commit()

        def update(**fields: Any) -> None:
            sets = ", ".join(f"{name} = ?" for name in fields)
            con.execute(f"UPDATE agent_runs SET {sets} WHERE id = ?", (*fields.values(), run_id))  # nosec B608
            con.commit()

        session_id = None
        try:
            session = client.create_session(settings["agent_id"], settings["environment_id"], budget,
                                            f"Data Donkey weekly growth: {town}")
            session_id = session["id"]
            update(session_id=session_id, status="running")
            client.send_message(session_id, prompt)
            status = _wait(client, session_id, settings["timeout_minutes"] * 60, poll_seconds, sleep)
            usage = client.get_session(session_id).get("usage") or {}
            cost = int((usage.get("list_cost") or {}).get("amount") or budget)
            update(status=status, cost_cents=cost)
            blobs = [(f.get("filename", ""), client.download(f["id"])) for f in client.list_files(session_id)
                     if str(f.get("filename", "")).casefold().endswith(".csv")]
            day = datetime.now(timezone.utc).date().isoformat()
            out = settings["home"] / "runs" / f"{day}-{_slug(town)}" / f"{_slug(town)}.csv"
            rows = _merge_csvs(blobs, out)
            snapshot = diff = None
            if rows:
                snapshot = growth.take_snapshot(out, f"weekly {town}", day, db_path=db_path, area=area)
                if not snapshot["duplicate"]:
                    diff = growth.diff_snapshots(new_id=snapshot["snapshot_id"], db_path=db_path)
            update(finished_at=_now(), files=len(blobs), rows=rows,
                   snapshot_id=snapshot and snapshot["snapshot_id"], changes=diff and diff["changes"])
            return {**result, "status": status, "session_id": session_id, "cost_cents": cost, "files": len(blobs),
                    "rows": rows, "csv": str(out), "snapshot": snapshot, "diff": diff}
        except Exception as exc:
            # No session means nothing was spent; otherwise the cost stays unknown and counts at the full budget.
            update(finished_at=_now(), status="error", note=str(exc)[:500],
                   **({} if session_id else {"cost_cents": 0}))
            raise
    finally:
        con.close()


def task_command(day: str = "SUN", at: str = "03:00", python: str | None = None) -> list[str]:
    """schtasks arguments that run the weekly runner once a week on Buck's PC."""
    if not re.fullmatch(r"MON|TUE|WED|THU|FRI|SAT|SUN", day.upper()):
        raise ValueError("day must be MON to SUN")
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
        raise ValueError("time must be HH:MM (24-hour)")
    run = f'"{python or sys.executable}" -m donkey growth-weekly'
    return ["schtasks", "/Create", "/F", "/SC", "WEEKLY", "/D", day.upper(), "/ST", at, "/TN", TASK_NAME, "/TR", run]


def install_task(day: str = "SUN", at: str = "03:00", dry_run: bool = False) -> int:
    command = task_command(day, at)
    if dry_run:
        print("Would run:", subprocess.list2cmdline(command))
        return 0
    if os.name != "nt":
        print("Windows only. Command:", subprocess.list2cmdline(command))
        return 1
    return subprocess.run(command, check=False).returncode  # nosec B603
