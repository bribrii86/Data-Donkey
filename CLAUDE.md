# Passdown: data-donkey (shared data store)
Read first. Update "Current state" in every PR you open.
## What this is
Data Donkey stores all data Brian Parris (bribrii86) acquires: city business lists, AZ SOS UCC filings, Overture
places, leads, research. Apps pull from it; the first is the Stephanie sales app (repo bribrii86/Sales-Pilot, working
name). Python package `donkey/` (standard library only), SQLite `~/DataDonkey/donkey.db` (`DONKEY_DB` overrides).
Docs: docs/data-donkey.md.
## Current state (2026-10-17)
- v0.6.0: who decides + board minutes (Buck 2026-10-17 17:54 UTC approved; research write-up in the project files
  research/decision_locality.md). Every call-this-week row carries `locality` (local, franchise, regional, corporate,
  central_procurement, unknown + evidence, confidence, owner_hint, initiative, source) and `board_note`. Free tier
  `donkey/locality.py` (public-body names, brands.json, Overture brand field now saved per place, same-name counts,
  NPI registry) settled 23% of the 5,778 places in her ZIPs. Paid weekly jobs: who-decides agent for unknown rows
  ($5 cap, 60 rows, 90-day cache) and board minutes from ~/DataDonkey/bodies.csv ($3 cap; board_minutes events score
  intent_plus 4, vendor 3). Monthly cap default $25 -> $40. New CLI: who-decides, import-brands. Needs bodies.csv
  (Buck's cloud researcher builds it) before the minutes job runs.
- v0.5.8: kiosk filter. call-this-week skips self-serve machines inside other stores (ATMs, Coinme/Coinstar/bitcoin
  kiosks, Redbox, ecoATM, Amazon Lockers, KeyMe; `triggers.looks_like_kiosk`, growth rows only; names with a company
  or service word like "ATM Repair LLC" still score). Buck 2026-10-12 20:19
  UTC: kiosks out, government offices (Pinal County Planning) stay. Buck's list after v0.5.7: 7 rows; Coinme drops.
- v0.5.7: growth diff noise fix. First real run (2026-10-11 23:34 UTC on Buck's PC; the 10:00 UTC Sunday run failed
  with "Agent not found", fixed in .env) put 24 Overture businesses on call-this-week, nearly all false: a chain's
  other store (Circle K, USPS, Banner) read as the first store moving, and suite or N/North spelling changes read as
  moves. Name+ZIP fallback now only matches an old row that vanished, moves compare house number + street name, and
  call-this-week counts each signal kind once per business. Same Overture diff: moves 235 to 72, list 24 to 6.
  `growth-diff --redo-all` recomputes stored diffs (Buck runs it once after upgrading).
- v0.5.6: PR #5 review fixes and the full AZCC Pinal paper list. Notices prompt now checks every paper the Commission
  approves for Pinal (Casa Grande Dispatch, Arizona Chinese American / Az Chinese News of Phoenix, Copper Basin News,
  San Manuel Miner, Superior Sun). Notices that give only the statutory agent's address score 3 as `new_llc_agent`
  with a "look it up first" reason. Holding-name filter only matches names that end in the street word ("2 Brothers
  Way Trucking LLC" scores). growth-weekly catches any error per job. DONKEY_NOTICES_CAP_USD under $1 now runs.
  PR #5 merged 2026-10-11 06:25 UTC at f4dfc3c (main 59e796b).
- v0.5.5: Pinal formation notices. `growth-weekly` now also runs a second agent session (own $1.50 cap, same $25 month)
  that reads the last 10 days of Pinal County new-LLC/corporation notices (Arizona requires them in a county paper;
  the ACC public notice list only has Maricopa and Pima) into `new_llc` events (source map `pinal-notices`, repeats
  dropped by entity ID). call-this-week: 3 points for 60 days; address-named or "holdings" LLCs don't score.
  `--no-notices`, `--notices-only`. Runner live on Buck's PC since 2026-10-11 00:47 UTC (Sundays 03:00); first real
  run 2026-10-11 10:00 UTC.
- v0.5.4: history sources. `python -m donkey overture-snapshot` stores each Overture release for an area (bbox now in
  areas.json) as snapshot "overture AREA" dated by the release and diffs it; S3 keeps only the latest releases
  (2026-08-19 and 2026-09-23 on 2026-10-10), so history builds from here. growth-weekly runs it first (free).
  Needs DuckDB (optional extra). Source map `sba-loans` (SBA 7(a)/504 FOIA CSVs, Arizona rows only via the map's
  new `keep` filter) adds `sba_loan` events; call-this-week scores loans approved in the last 180 days (3 points). Review fixes: Overture changes are dated
  the save day and score lower (only home-to-office reaches 3); sba-loans refuses files missing name, state or
  approval-date columns, and a file that keeps 0 rows is rolled back (not marked ingested).
- v0.5.3: weekly growth runner `python -m donkey growth-weekly` (`donkey/runner.py`):
  Console research agent per town (rotation from areas.json), hard per-session budget, $25/month cap in the
  `agent_runs` table, call log in ~/DataDonkey/logs, then snapshot + diff. `--install-task` registers the Windows task.
  Buck approved the spend 2026-10-10 20:14 UTC. Not yet run for real (needs agent and environment IDs in .env).
- v0.5.2: areas.json (stephanie, pinal, phoenix-metro, tucson-metro, arizona; DONKEY_AREAS overrides); UCC monthly
  index source map `az-sos-ucc-index` (tsv, guessed column names, ingest reports unmapped fields); UCC lessor tags and
  lease-end estimates (`donkey/ucc.py`); `call-this-week` ranking (`donkey/triggers.py`, `api.call_this_week`, app
  scorer hooks). No SOS index bought yet; no paid API calls. Review fixes: a pre-v0.5.2 donkey.db gets its UCC
  filings tagged when opened (`ucc.backfill`); `--area` runs keep SOS CSV filings that have no city or ZIP (town from
  debtor name, else "location unknown"). data/ucc.csv tags 9 filings as copier_lessor.
- v0.5.1: growth tracking. `python -m donkey growth-snapshot` stores a dated copy of a business list;
  `growth-diff` turns changes (home to office, move, new business, new website/phone) into `growth_*` events with
  points; `donkey.api.growth()` ranks businesses for the Monday list. Plan: Sales-Pilot project files
  data-donkey/growth-tracker-plan-2026-10-10.md. Not yet wired into the Sales-Pilot Monday list.
- v0.5.0: split out of the Sales-Pilot repo (was Data-Donkey) with history; no longer imports the app
  (`donkey/common.py` holds the name and date helpers); pip-installable (pyproject.toml); default database moved to
  the user's home folder.
- Sales-Pilot ships the wheel (`python -m build --wheel`) and installs it in setup.bat. Rebuild and copy the wheel
  there when donkey changes.
- Next: Sales-Pilot Monday list reads api.call_this_week(); second weekly snapshot (first real diff); first SOS
  index file (fix column names in the map); more imports (Casa Grande city list and other city lists).
## Rules for every agent
1. Push before replying; confirm local HEAD equals remote branch head and report that SHA. No local-only branches or commits.
2. Branches start with copilot/ (Copilot) or claude/ (Claude sessions). Review fixes go on the same PR branch.
3. Merge only the reviewed SHA.
4. Version lives in code (`donkey/__init__.py`, version-check test); never create a VERSION file. Tag only on main after merge.
5. Run pytest, ruff, bandit, pip-audit and report counts. Never push to main; PR body begins "Requested by Brian Parris (bribrii86)".
6. Keys live only in environment variables or a gitignored .env, never in the repo. Real API calls are allowed on the owner's machine with a spend cap and call logging; tests use fixtures only. Public data (city lists, permits) may be pulled. No real customer data (accounts, transcripts) in the repo, fixtures or PRs. Never touch BuckAI.
7. Never put angle brackets in prompts for Copilot; use UPPERCASE placeholders.
