# Passdown: data-donkey (shared data store)
Read first. Update "Current state" in every PR you open.
## What this is
Data Donkey stores all data Brian Parris (bribrii86) acquires: city business lists, AZ SOS UCC filings, Overture
places, leads, research. Apps pull from it; the first is the Stephanie sales app (repo bribrii86/Sales-Pilot, working
name). Python package `donkey/` (standard library only), SQLite `~/DataDonkey/donkey.db` (`DONKEY_DB` overrides).
Docs: docs/data-donkey.md.
## Current state (2026-10-22)
- v0.6.12 (Buck "go on both" 2026-10-22 23:02 UTC): a new_license doesn't score when the same business
  held a license more than 60 days earlier (same row, or same trade name + ZIP; an owner's second business still scores). Casa Grande sent its 2025 list (reply to the
  #26-330 follow-up; project files data/casa_grande/casa_grande_licenses_2025.csv, 1,229 rows, 1,203 phones): loading
  it under city-licenses fills phones on ~1,016 of the 2026 businesses and drops 18 of the 55 Aug-Oct 2026 licenses
  that were existing businesses.
- v0.6.11: public-lease-calendar sorts free-text openings by the earliest year from this year on ("no council item
  since 2013" = unknown, "~Sep 2030" = 2030-09). Buck's re-import 2026-10-22 17:18 UTC brought the dates in; the old
  sort put 2013/2024 history first.
- v0.6.10: `public-leases-import` reads Buck's hand-built list headers (found, meeting_date, est_next_opening,
  amount, ucc_filing). First import on his PC 2026-10-22 17:12 UTC (28 bodies) left every next opening blank; he
  re-imports after this (the newest row per body wins).
- v0.6.9 (Buck "go" 2026-10-22 11:53 UTC, Fable market review's top pick): public-body copier lease calendar for any
  county. `public-leases --county X --bodies FILE` (paid, capped: DONKEY_PUBLIC_LEASES_CAP_USD $25 per run, 15 bodies
  per agent session, not in growth-weekly) reads each body's minutes for its copier lease into `public_lease` events
  (source map `public-leases`; AZ bodies get their UCC filing months; bodies found in the last 365 days skipped).
  `public-leases-import` stores a hand-built list (free); `public-lease-calendar --county X --out STEM` writes CSV +
  XLSX by next opening; `bodies-prompt --county X` prints the research prompt that builds a county's bodies CSV.
  No paid run yet; Pinal re-run estimate $15-25.
- v0.6.8 (Buck yes 2026-10-22 00:20 UTC): building owners. `parcels-pull --area stephanie` (free, no key; also a
  free step in growth-weekly) stores non-residential parcels from the City of Maricopa ArcGIS layer TaxParcel_8_26
  (all Pinal, owner + mailing address, year built, last sale) in `parcels`. call-this-week rows get `property` and
  `property_note` (one owner at the same ZIP + house number + street). Only owns-its-building (owner = the business name, or the
  same 2+ words once town and landlord words like Holdings/Trust are dropped) + mailing state not AZ turns an unknown
  who-decides into corporate; a landlord's address changes nothing. A pull that comes back empty or under half the
  stored rows keeps the old parcels; paging stops at 50 pages or a repeated page; unreadable rows are skipped.
  Cloud test 2026-10-22: 3,643 parcels in her ZIPs; 13 of the 30 Casa Grande license rows matched an owner.
- v0.6.7 (Buck yes 2026-10-21 01:40 UTC; his 2026-10-20 list had addresses on all 30 license rows but no phones):
  a blank phone/website/address comes from the latest listings snapshot (`triggers._from_listings`): like name, same
  ZIP, same house number + street; with no street, the one same-name place in the ZIP; two candidates = blank.
- v0.6.6 (Buck yes 2026-10-20 00:50 UTC, after his first Monday list with license rows had no address or phone):
  every call-this-week row carries `address`, `phone`, `website` (`triggers._contacts`: the event's business row, else
  same name + ZIP with a phone first; a license's own street when no address; none for agent-address notices).
- v0.6.5 (stacked on v0.6.4 / PR #13; Buck chose "Yes, 60 days" 2026-10-19 16:17 UTC): source map `city-licenses`
  (city-business-list + Issue Date as a `new_license` event); call-this-week scores a license 3 points for 60 days,
  never one dated Jan 1 (annual renewals), a kiosk (ECOATM LLC) or a home business (person's legal name at an
  apartment/lane/court/drive address; Buck yes 2026-10-19 23:21 UTC). parse_date reads MM-DD-YYYY. First file: Casa Grande NextRequest #26-330
  (1,176 licenses, 2026 only; project files data/casa_grande/, never the repo). Same bytes already ingested under
  another map are refused with a note naming it (batches are unique by SHA-256 across maps).
- v0.6.4: phone and website match keys (Buck flagged Fable's business-plan gap 2026-10-19 13:11 UTC). businesses gets
  `phone_key` (10 digits, no toll-free) and `domain_key` (host, no social/site-builder hosts); city-business-list (Business
  Phone, Website) and both Overture maps map them. Only same-name row + same town + new address + same phone = moved;
  a same-name tie is broken by exactly one shared phone or domain; a different spelling needs phone AND domain plus
  shared name words and same ZIP. An older donkey.db gets the columns and keys from stored rows when opened, in one
  transaction (`ingest._add_match_keys`). Sonnet review fixes: no merges of clinic tenants or chain stores.
- v0.6.3: `crustdata-companies` source map (Buck said yes 2026-10-19 05:53 UTC). A Crustdata company export (CSV,
  LinkedIn-based) is stored as `headcount_growth` events (Arizona rows only, town from headquarters, dated the import
  day); call-this-week gives the `hiring` boost to a listed business whose headcount grew (name + town match, 180
  days). Buck's first export (trial credits, 2026-10-19): 1,550 rows, 523 in her towns, 125 of those grew.
- v0.6.2 (stacked on v0.6.1 / PR #10; Buck said yes 2026-10-19 01:43 UTC): `lease-calendar` CLI + `api.lease_calendar()`
  (copier leases likely ending by month, from the UCC estimates). call-this-week boosts on listed rows only:
  `print_heavy` +1 (law, accounting, medical, real estate/title, insurance, schools) and `hiring` +2 (who-decides
  initiative mentions hiring or expansion; that prompt now also searches job posts).
- v0.6.1: `crawl-ab` (Buck asked 2026-10-18 23:41 UTC). Optional Crawl4AI fetch layer (`donkey/crawl.py`, extra
  `crawl`) for a one-town A/B against the agent's last run of that town: same pages, Crawl4AI Markdown + one Messages
  call per page, report of cost/tokens/rows. Cap DONKEY_CRAWL_CAP_USD $2, model DONKEY_CRAWL_MODEL (default Opus 5.5).
  Weekly job unchanged; no snapshots written. Cloud smoke test 2026-10-18: a Yellow Pages page went 295k HTML chars to
  41k Markdown; maricopa-az.gov (403) and chamberofcommerce.com (Cloudflare) refused a datacenter IP and were skipped.
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
