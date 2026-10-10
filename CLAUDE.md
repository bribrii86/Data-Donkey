# Passdown: data-donkey (shared data store)
Read first. Update "Current state" in every PR you open.
## What this is
Data Donkey stores all data Brian Parris (bribrii86) acquires: city business lists, AZ SOS UCC filings, Overture
places, leads, research. Apps pull from it; the first is the Stephanie sales app (repo bribrii86/Sales-Pilot, working
name). Python package `donkey/` (standard library only), SQLite `~/DataDonkey/donkey.db` (`DONKEY_DB` overrides).
Docs: docs/data-donkey.md.
## Current state (2026-10-10)
- v0.5.0: split out of the Sales-Pilot repo (was Data-Donkey) with history; no longer imports the app
  (`donkey/common.py` holds the name and date helpers); pip-installable (pyproject.toml); default database moved to
  the user's home folder.
- Sales-Pilot ships the wheel (`python -m build --wheel`) and installs it in setup.bat. Rebuild and copy the wheel
  there when donkey changes.
- Next: v0.5.x more imports (Casa Grande city list and other city lists).
## Rules for every agent
1. Push before replying; confirm local HEAD equals remote branch head and report that SHA. No local-only branches or commits.
2. Branches start with copilot/ (Copilot) or claude/ (Claude sessions). Review fixes go on the same PR branch.
3. Merge only the reviewed SHA.
4. Version lives in code (`donkey/__init__.py`, version-check test); never create a VERSION file. Tag only on main after merge.
5. Run pytest, ruff, bandit, pip-audit and report counts. Never push to main; PR body begins "Requested by Brian Parris (bribrii86)".
6. Keys live only in environment variables or a gitignored .env, never in the repo. Real API calls are allowed on the owner's machine with a spend cap and call logging; tests use fixtures only. Public data (city lists, permits) may be pulled. No real customer data (accounts, transcripts) in the repo, fixtures or PRs. Never touch BuckAI.
7. Never put angle brackets in prompts for Copilot; use UPPERCASE placeholders.
