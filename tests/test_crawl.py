"""v0.6.1: Crawl4AI A/B with a fake fetcher and a fake Messages client. No network, no browser, no paid calls."""
import json

import pytest

from donkey import crawl, runner
from donkey.ingest import connect, main

AGENT_CSV = ("name,street,city,zip,phone,website,category,home_based,source_url,date_seen\n"
             "Desert Rose Plumbing,123 W Sunny Ln,Maricopa,85138,520-555-0101,,Plumber,yes,https://dir.test/a,2026-10-11\n"
             "Copper Sky Dental,9 W Main St Ste 2,Maricopa,85138,,,Dentist,no,https://dir.test/b,2026-10-11\n")
REPLY = ("```csv\nname,street,city,zip,phone,website,category,home_based,source_url,date_seen\n"
         "Desert Rose Plumbing LLC,123 W Sunny Lane,Maricopa,85138,520-555-0101,,Plumber,yes,x,x\n"
         "Saguaro Signs,77 N Main St,Maricopa,85138,520-555-0177,,Signs,no,x,x\n```")


class FakeMessages:
    def __init__(self, reply=REPLY):
        self.reply, self.prompts = reply, []

    def create(self, model, prompt):
        self.prompts.append(prompt)
        return self.reply, {"input_tokens": 10_000, "output_tokens": 500}


def fake_fetch(urls):
    pages = {"https://dir.test/a": {"ok": True, "status": 200, "html_chars": 50_000,
                                    "markdown": "![logo](x.png)\n\n\n\nDesert Rose Plumbing 520-555-0101"},
             "https://dir.test/b": {"ok": False, "status": 403, "html_chars": 0, "markdown": ""}}
    return [{"url": u, **pages.get(u, {"ok": True, "status": 200, "html_chars": 10, "markdown": "Please verify you "
                                                                                                    "are human"})}
            for u in urls]


def _settings(tmp_path, **env):
    return runner.load_settings({"ANTHROPIC_API_KEY": "test-key-not-real", **env}, home=tmp_path)


def _agent_run(tmp_path, db):
    run = tmp_path / "runs" / "2026-10-11-maricopa"
    run.mkdir(parents=True)
    (run / "maricopa.csv").write_text(AGENT_CSV, encoding="utf8")
    con = connect(db)
    con.execute("INSERT INTO agent_runs (started_at, area, town, session_id, status, budget_cents, cost_cents, rows) "
                "VALUES ('2026-10-11T10:00:00+00:00', 'stephanie', 'Maricopa', 'sesn_1', 'done', 600, 412, 2)")
    con.commit()
    con.close()


def test_clean_markdown_drops_images_and_blank_runs():
    assert crawl.clean_markdown("![a](b.png)Hi\n\n\n\n[](x)There  \n") == "Hi\n\nThere"


def test_fetch_pages_skips_refused_and_captcha_pages():
    pages = crawl.fetch_pages(["https://dir.test/a", "https://dir.test/b", "https://dir.test/c"], fake_fetch)
    assert [p["skipped"] for p in pages][0] == ""
    assert "refused" in pages[1]["skipped"] and "refused" in pages[2]["skipped"]
    assert pages[0]["markdown"].startswith("Desert Rose")


def test_run_ab_compares_with_the_agent_run_and_records_spend(tmp_path):
    db = tmp_path / "donkey.db"
    _agent_run(tmp_path, db)
    fake = FakeMessages()
    result = crawl.run_ab(_settings(tmp_path), "Maricopa", db_path=db, client=fake, fetch=fake_fetch)
    assert result["status"] == "done" and result["pages"] == 2 and result["pages_skipped"] == 1
    assert len(fake.prompts) == 1 and "https://dir.test/a" in fake.prompts[0]
    assert result["crawl"]["input_tokens"] == 10_000 and result["crawl"]["cost_cents"] == 5.0  # Opus 5.5 list price
    cmp = result["compare"]
    assert (cmp["agent_businesses"], cmp["crawl_businesses"], cmp["both"], cmp["only_crawl"]) == (2, 2, 1, 1)
    assert result["agent"]["cost_cents"] == 412
    rows = (tmp_path / "runs").glob("*-ab-maricopa/crawl4ai.csv")
    assert "Saguaro Signs" in next(rows).read_text(encoding="utf8")
    con = connect(db)
    try:
        ab = con.execute("SELECT * FROM agent_runs WHERE town = 'crawl4ai A/B: Maricopa'").fetchone()
        assert ab["cost_cents"] == 5 and ab["session_id"] is None and ab["status"] == "done"
        assert con.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 0  # weekly diff untouched
    finally:
        con.close()
    assert json.loads((tmp_path / "runs").glob("*-ab-maricopa/report.json").__next__().read_text())["status"] == "done"


def test_run_ab_dry_run_makes_no_claude_call_and_no_row(tmp_path):
    db = tmp_path / "donkey.db"
    _agent_run(tmp_path, db)
    fake = FakeMessages()
    result = crawl.run_ab(_settings(tmp_path), "Maricopa", dry_run=True, db_path=db, client=fake, fetch=fake_fetch)
    assert result["status"] == "dry_run" and not fake.prompts and result["worst_case_cents"] > 0
    con = connect(db)
    try:
        assert con.execute("SELECT COUNT(*) FROM agent_runs").fetchone()[0] == 1
    finally:
        con.close()


def test_run_ab_stops_at_the_cap(tmp_path):
    db = tmp_path / "donkey.db"
    urls = tmp_path / "urls.txt"
    urls.write_text("https://dir.test/a\nnot a url\n", encoding="utf8")
    fake = FakeMessages()
    result = crawl.run_ab(_settings(tmp_path, DONKEY_CRAWL_CAP_USD="0.01"), "Maricopa", urls, db_path=db,
                          client=fake, fetch=fake_fetch)
    assert not fake.prompts and "cap" in result["crawl"]["stopped"]


def test_run_ab_cheaper_model_from_env(tmp_path):
    db = tmp_path / "donkey.db"
    _agent_run(tmp_path, db)
    result = crawl.run_ab(_settings(tmp_path, DONKEY_CRAWL_MODEL="claude-haiku-5-5"), "Maricopa", db_path=db,
                          client=FakeMessages(), fetch=fake_fetch)
    assert result["model"] == "claude-haiku-5-5" and result["crawl"]["cost_cents"] == pytest.approx(0.125)


def test_run_ab_without_pages_explains(tmp_path):
    with pytest.raises(ValueError, match="--urls"):
        crawl.run_ab(_settings(tmp_path), "Eloy", db_path=tmp_path / "d.db", client=FakeMessages(), fetch=fake_fetch)


def test_cli_crawl_ab_reports_missing_pages(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DONKEY_HOME", str(tmp_path))
    monkeypatch.setenv("DONKEY_DB", str(tmp_path / "d.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    assert main(["crawl-ab", "--town", "Eloy", "--dry-run"]) == 1
    assert "no pages to fetch" in capsys.readouterr().err
