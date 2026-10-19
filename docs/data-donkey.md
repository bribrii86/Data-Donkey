# Data Donkey

Data Donkey is the shared data store for acquired business lists, UCC filings,
Overture places and leads. It is a standalone `donkey` Python package (standard
library only) and SQLite database. Apps such as the Stephanie sales app
(repo Sales-Pilot) install the package and read the database.

## Layout and database

- `donkey/` contains the schema, ingestion CLI, read-only query API and source maps.
- `donkey/sources/*.json` maps each source's input columns to common fields.
- `donkey.db` lives in the user's home folder by default (`~/DataDonkey/donkey.db`,
  on Windows `C:\Users\NAME\DataDonkey\donkey.db`), so every app on the computer
  reads the same file. Set `DONKEY_DB` to use a different file. Database files are
  ignored by Git.
- `samples/city-business-list.csv` is synthetic data for a city-list example.

The SQLite schema has six tables:

- `sources`: source identifiers and descriptions.
- `batches`: each ingested file, SHA-256, receipt time and sender, plus source row count.
- `businesses`: normalized business identities and searchable location/vertical details.
- `records`: every original source row as JSON, its batch, match status and optional business link.
- `events`: dated business events such as `ucc_filing`.
- `contacts`: business contacts carried by imported source rows.

Identical file contents (the same SHA-256) are only ingested once, even if the
file name changes. Names are normalized with `donkey.common.norm` (copied from the
Stephanie app's `prospects._norm` at the v0.5.0 split; same results for ACP). A row is matched only when normalized names and ZIP or street
agree, or (v0.6.4) the phone and website keys settle it (see the last section). Ambiguous or insufficiently located same-name rows are retained in
`records` with `match_status='needs_review'`; they are never auto-merged. Use
`donkey.api.needs_review()` to retrieve that list.

The read-only functions in `donkey.api` are `search(city, vertical)`,
`events(kind, towns, since)`, and `lookup(name)`. They return lists of dictionaries
and accept an optional `db_path` for applications and tests.

## Ingest a new city list

1. Copy `donkey/sources/city-business-list.json` to a lower-case, hyphenated
   source ID (for example, `casa-grande-business-list.json`).
2. Set the map's `id` to the file name without `.json`, give it a descriptive
   `name`, and change the `columns` values to the exact column names in the
   city's CSV. Each canonical field may map to one column name or a list of
   candidate column names. Supported canonical business fields include `name`,
   `address`, `street`, `city`, `state`, `zip`, `vertical`, `license_number`,
   `open_date`, and `naics`; optional contact fields are also mapped in the
   generic city-list map.
3. Ingest the file and identify its sender:

   ```console
   python -m donkey ingest --source casa-grande-business-list --file samples/city-business-list.csv --received-from "City of Casa Grande"
   ```

4. Review the printed new, matched and needs-review counts. Inspect the persisted
   review rows with `donkey.api.needs_review()`. The committed sample is synthetic;
   do not add real customer lists or records to the repository or test fixtures.

CSV, JSON arrays, and JSONL inputs are supported. Existing maps include
`az-sos-ucc` (creates `ucc_filing` events), `overture-places`, and the generic
`city-business-list`.

## Install

```console
pip install -e .            # from a checkout, for development
pip install -e ".[dev]"     # plus pytest, ruff, bandit, pip-audit, build
python -m build --wheel     # dist/data_donkey-VERSION-py3-none-any.whl for apps to ship
```

Apps call `donkey.api` (read-only) and `donkey.api.database_path()` to find the file.

## The Stephanie app reads Data Donkey

Since app v0.4.1, `prospects.load_ucc_filings()` uses `ucc_filing` events from
`donkey.db` when it has any, else the app's `data/ucc.csv`. Fake mode ignores Data
Donkey. The app ships the Data Donkey wheel and installs it in `setup.bat`.

## Copy the app's Overture places

```console
python -m donkey ingest-overture --history-db PATH
```

Copies the `overture_places` table from the app's `history.db` (default: `STEPHANIE_DB`; with neither, the
command stops and asks for `--history-db`) into the
`overture-history-db` source. The batch records the sha256 of the table contents; the same contents are never
ingested twice, and every row is kept in `records` as JSON.

## Doctor

`python -m stephanie_assistant.cli doctor` prints the `donkey.db` path, row counts per source and when the newest
batch was received. A missing `donkey.db` is a WARN, not a FAIL.

## Growth tracking (v0.5.1)

Store a dated copy (snapshot) of any business list, then diff it against the
previous copy with the same label. Each change becomes a `growth_*` event:

```console
python -m donkey growth-snapshot --file casa-grande-offices-2026-10-17.csv --label "casa grande offices"
python -m donkey growth-diff
```

- Columns: `name, street, city, zip, phone, website, category`, optional
  `home_based` and `date_seen` (the snapshot date; else `--taken-at`, else today).
- Rows match across snapshots on phone + name, else name + ZIP, but only to an old row that has no match of its
  own: a chain's second store in the same ZIP is a new location, not the first store moving (v0.5.7).
- A move means a different house number or street name. A suite added or renumbered, a direction (N/North) or a
  leading place name ("Promenade at Casa Grande, ...") is not a move (v0.5.7).
- Events and points: `growth_home_to_office` 5 (home address to a suite or
  commercial street), `growth_new_business` 3, `growth_moved` 3,
  `growth_new_website` 1, `growth_new_phone` 1, `growth_category_change` 1.
  Rows that disappear are not reported (usually a missed page, not a closure).
- `home_based` is a guess when the file lacks the column: a PO box, or a trade
  (plumber, landscaper, cleaner...) at a street address with no suite that is
  not on an arterial (Pkwy, Blvd, Hwy, Pinal Ave, Maricopa Rd, Main St).
- `donkey.api.growth(towns, since, min_score=3)` sums points per business,
  highest first. The Monday list shows these as "call this week".
- Snapshot and diff are stored once each (SHA-256 / snapshot pair).
- `python -m donkey growth-diff --redo-all` recomputes every stored diff with the current rules (same snapshots,
  event dates and scores). Run it once after upgrading to v0.5.7.
- `call-this-week` counts each kind of signal once per business, so three kiosks of one chain in a ZIP are one
  "new business", not three (v0.5.7).
- `call-this-week` skips growth signals from self-serve machines inside other stores (ATMs, Coinme/Coinstar/bitcoin
  kiosks, Redbox, ecoATM, Amazon Lockers, KeyMe): no office to sell to. A name with a company or service word
  ("ATM Repair LLC") still scores, and so do notices, SBA loans and UCC filings. Government offices score (v0.5.8).

## Areas, UCC monthly index and "call this week" (v0.5.2)

**Areas.** `donkey/areas.json` names the sales areas: `stephanie` (her 12 ZIPs and towns), `pinal`,
`phoenix-metro` (ZIP prefixes 850/852/853), `tucson-metro` (856/857) and `arizona`. The non-Stephanie lists are
approximate; edit the file, or point `DONKEY_AREAS` at a replacement for another dealer. `growth-snapshot --area`
keeps only rows in an area; `api.growth(area=...)` and `call-this-week --area` filter by it.

**UCC monthly index.** The source map `az-sos-ucc-index` reads the SOS Monthly Filing Index (tab-separated). Its
column names are guesses until the first file arrives: each field lists candidate header names, and ingest prints the
fields it could not find ("not found in this file") so the real names can be added to the map.

```console
python -m donkey ingest --source az-sos-ucc-index --file UCC_INDEX_2026_11.txt --received-from "AZ SOS"
```

Every `ucc_filing` event (from the index or the older `az-sos-ucc` CSV) now carries `name`, `zip` and tags from
`donkey/ucc.py`: `lessor_type` (copier_lessor, equipment_finance, bank, mca, ag, auto, government, other) from the
secured party's name, and for lessors `lease_window_start` (filing + 36 months) and `lease_end_est` (filing + 60
months). Arizona records no collateral, so these are estimates from the lender's name. A database ingested before
v0.5.2 gets these tags the next time any write command opens it (re-ingesting the same file is skipped by its hash,
so this backfill is what tags the old filings); `call-this-week` also tags untagged filings on the fly.

**Call this week.** `donkey.api.call_this_week(area, as_of, days=14, min_score=3, scorers=())` ranks businesses by
adding points from every trigger in the window: growth events (above), a new filing with a copier lessor (4),
equipment finance (3), bank or other (1), and a copier lease whose estimated end is within 180 days (5). Each row has
`score`, `signals` and plain-language `reasons`. Apps add their own triggers by passing scorer functions (for
example a permit feed); hits for the same business name and ZIP are added together. Scores live in
`donkey.triggers.SCORES`.

The older SOS CSV (`az-sos-ucc`) has no city or ZIP. In an area run those filings are kept: when the debtor name
contains one of the area's towns (not "TOWN County") that town is shown, otherwise the row says "location
unknown" for the rep to check. Each hit's `details` carry `location`: known, debtor name, or unknown.

```console
python -m donkey call-this-week --area stephanie
```

## Weekly growth runner (v0.5.3, paid)

`python -m donkey growth-weekly` asks Buck's Console research agent (Managed Agents API) to list every business in
one town, downloads the per-page CSVs it saves, merges them into `~/DataDonkey/runs/DATE-TOWN/TOWN.csv`, then runs
growth-snapshot (label `weekly TOWN`, area filter) and growth-diff. Growth events then show up in `call-this-week`.

- Rotation: one town per run through the area's `cities` in areas.json (default area `stephanie`, 12 towns); towns
  never run go first, then the one run longest ago. `--town NAME` overrides.
- Spend: each session gets a hard platform budget of `min(DONKEY_RUN_CAP_USD, monthly cap left)`. The monthly cap
  (`DONKEY_MONTHLY_CAP_USD`, default 25) is the sum of `agent_runs.cost_cents` this UTC month; a run without a final
  cost counts at its full budget. Under $1 left: the run is skipped and logged, with no API call.
- Call log: every HTTP call (method, path, status, seconds; never the key) goes to
  `~/DataDonkey/logs/console-calls.jsonl`. Every run, skip or error is a row in the `agent_runs` table.
- Unattended: paused tool calls are denied; custom tools get "not available"; after `DONKEY_RUN_TIMEOUT_MINUTES`
  (default 90) the session is interrupted and whatever pages were saved are used.
- `--dry-run` prints the town, budget left and prompt with no API call.

Settings (environment, or `~/DataDonkey/.env`, which is outside the repo; `DONKEY_ENV_FILE` points elsewhere):

```
ANTHROPIC_API_KEY=...
DONKEY_AGENT_ID=agent_...          # the Console research agent
DONKEY_ENVIRONMENT_ID=env_...      # its environment
DONKEY_MONTHLY_CAP_USD=25
DONKEY_RUN_CAP_USD=6
```

Schedule (Windows): `python -m donkey growth-weekly --install-task` registers "DataDonkey Growth Weekly" for Sunday
03:00 (`--day`, `--time` to change). Tests use a fake client only.

## History sources: Overture releases and SBA loans (v0.5.4, free)

Older records make the diff useful right away instead of after weeks of snapshots.

**Overture.** `python -m donkey overture-snapshot [--area stephanie] [--release 2026-09-23.0]` downloads every
Overture release not yet stored for the area (bbox in areas.json, then kept by ZIP or town), saves
`~/DataDonkey/overture/overture-RELEASE-AREA.csv`, stores it as snapshot `overture AREA` dated by the release, and
diffs it with the previous release. Overture only keeps its latest releases on S3 (two on 2026-10-10), so this
history starts now and grows monthly; `growth-weekly` runs this check first on every scheduled run. Needs DuckDB
(`pip install duckdb`; the Sales-Pilot install already has it). Changes are dated the day they are saved, so a
backfilled release shows on this week's list. Overture scores are lower than our own scrape (home to office 3, moved
2, new business, website or phone 1, category 0) because many "new businesses" are only Overture adding coverage:
only a home-to-office move reaches call-this-week's 3 points on its own.

Source maps can list `required` fields: a file missing any of those columns is refused before anything is recorded,
and a file whose `keep` filter keeps 0 rows is rolled back, so in both cases it can be ingested again after a fix.

**SBA loans.** Download the 7(a) and 504 FOIA CSVs from data.sba.gov (fiscal 1991 to now, quarterly), then
`python -m donkey ingest --source sba-loans --file FILE.csv --received-from data.sba.gov` for each. Only Arizona rows
are stored (`keep` in the source map); the rest are counted as "left out". Each loan is an `sba_loan` event (amount,
lender, industry, jobs, business age). `call-this-week` gives 3 points to a loan approved in the last 180 days; older
loans stay as history (who borrowed, when, how much). Column names follow the 2020s file layout; ingest lists any it
can't find.

## Pinal formation notices (v0.5.5, paid, part of the weekly run)

Arizona requires a new LLC or corporation to publish a notice of formation in a newspaper of the county of its
statutory agent or known place of business (A.R.S. 29-3201, 10-203). Only Maricopa and Pima filers are posted on the
Corporation Commission's public notice list, so new Pinal businesses show up only in Pinal newspapers. That notice is
usually the earliest public sign of a new business, weeks before a permit or a directory listing.

`growth-weekly` now runs a second agent session after the town run: it reads the last 10 days of Pinal formation
notices (arizonapublicnotices.com with the Pinal filter, the Casa Grande Valley Newspapers Column sites, then every
paper the Corporation Commission approves for Pinal notices: Casa Grande Dispatch, Arizona Chinese American / Az
Chinese News (a Phoenix paper also approved for Pinal), Copper Basin News, San Manuel Miner and Superior Sun) and saves `pinal-notices-NN.csv`. Rows already stored (same entity ID, else same name) are dropped, the
rest go to `~/DataDonkey/runs/DATE-pinal-notices/pinal-notices.csv` and are ingested with source map `pinal-notices`
as `new_llc` events. The address is the business's when the notice gives one, else the statutory agent's
(`address_type`).

`call-this-week` gives 3 points to a new LLC or corporation formed in the last 60 days. LLCs named after a street
address ("408 N. Sacaton Street LLC") or called holdings are kept in the store but don't score: they are usually a
building's owner (the name must end with the street word, so "2 Brothers Way Trucking LLC" still scores). Notices
outside the area (by ZIP or town) don't score either. When the notice gives only the statutory agent's address
(often a lawyer's office), the row still scores 3 but its reason says the address is the agent's, so look the
business up before calling (v0.5.6).

Cost: its own per-run cap `DONKEY_NOTICES_CAP_USD` (default $1.50; a cap under $1 also works) inside the same
monthly cap. A failure in one job
never stops the other. `--no-notices` skips it; `--notices-only` runs only it; `--dry-run` prints both prompts.
Known gap: a Pinal business whose statutory agent is in Maricopa or Pima publishes there (no address on that list),
so it is missed here.

## Who decides and board minutes (v0.6.0)

Stephanie drops a business at once when it can't buy office equipment locally, so every call-this-week row now
carries `locality`: `value` (local, franchise, regional, corporate, central_procurement, unknown), `evidence`,
`confidence`, `owner_hint` (likely decision maker), `initiative` and `source`, plus `board_note` (latest public
meeting notes for a public body). Stored in the `locality` table, keyed by normalized name + ZIP (else town), with
the raw evidence in `details_json`; an answer is reused for 90 days (`locality.CACHE_DAYS`).

Free tier (`python -m donkey who-decides`, also run inside the weekly job):
- public bodies by name (city, county, school district, college, tribal, fire, courts, USPS): central_procurement;
- `donkey/brands.json`: hand-kept corporate and franchise brands (seeded from the brands Overture lists in
  Stephanie's ZIPs). `python -m donkey import-brands --file nsi.json` adds the OpenStreetMap Name Suggestion Index
  as plain chains. One-word and imported brands only match with a store word or number after them
  ("Target Pest Control" is not Target), unless Overture's brand field agrees;
- Overture's brand field per place (`place_brands`, saved with each release; re-run
  `overture-snapshot --release NAME` to backfill a stored release): 13.7% of the 5,778 places in her ZIPs had one in
  2026-09-23.1, and it is noisy, so alone it only gives regional/low;
- places in the area sharing the name: 3+ regional, 8+ corporate;
- the free NPI registry for clinics the rules leave unknown: same legal name at 3+ Arizona locations or a subpart is
  regional, 8+ corporate; an authorized official titled owner/president/DDS/MD with 1-2 locations is local, and the
  official becomes the owner hint.
On all 5,778 places in her ZIPs the rules alone settled 23% (531 corporate, 246 franchise, 359 regional, 221 public).

Paid tier (`runner.run_locality`, weekly, own cap `DONKEY_LOCALITY_CAP_USD`, default $5; at most
`DONKEY_LOCALITY_MAX_ROWS`, default 60): one Console agent session for this week's rows that are still unknown or
older than 90 days. It reads the site footer, About and Locations pages, searches for a parent, checks the ACC record
(principals; a law-firm or agent-service statutory agent is never the buyer) and notes any public initiative. Its CSV
is kept in `runs/DATE-who-decides/`.

Board minutes (`runner.run_minutes`, weekly, own cap `DONKEY_MINUTES_CAP_USD`, default $3): reads the public-body
list at `DONKEY_BODIES_FILE` (default `~/DataDonkey/bodies.csv`, the researcher's decision_bodies CSV: body_name,
body_type, zip_or_area, portal_platform, agendas_url, minutes_url). No list, no run. The agent keeps agenda items about
copiers, print, document management, IT, phones, RFPs, co-op contracts and related budgets, flagged intent_plus
(4 points), vendor (3), intent_minus or info (0, shown as a reason only). Stored as `board_minutes` events (source map
`board-minutes`, repeats dropped by link + summary) and scored for 45 days.

The monthly cap default is now $40 (Buck approved the two new jobs 2026-10-17). `growth-weekly --no-minutes` and
`--no-locality` skip them.

## Crawl4AI A/B for the town scrape (v0.6.1, paid, capped, manual only)

`python -m donkey crawl-ab --town Maricopa` tests a second way to read a town: Crawl4AI (free, open source, a local
headless browser) fetches the same directory pages the agent used last time for that town (the `source_url` values in
`~/DataDonkey/runs/DAY-SLUG/SLUG.csv`), turns each into clean Markdown, and one Messages API call per page pulls the
business rows out. It then compares with that agent run: Claude cost and tokens, businesses found by each, by both,
and how many have a phone and street. Output: `~/DataDonkey/runs/DAY-ab-SLUG/crawl4ai.csv` and `report.json`. Nothing
goes into snapshots, so the weekly diff and call-this-week are untouched; the weekly job does not change.

- Install once: `pip install "data-donkey[crawl]"` then `crawl4ai-setup` (downloads its browser).
- `--dry-run` fetches the pages (free) and shows page sizes and the worst-case Claude cost, with no Claude call.
- `--urls FILE` uses your own page list (one URL per line) instead of the agent's pages.
- `--agent-usage` also asks the Console for the agent session's own token counts.
- Cap: `DONKEY_CRAWL_CAP_USD` (default $2) and the month cap; the run is one `agent_runs` row ("crawl4ai A/B: TOWN",
  no session, so the town rotation ignores it). Before each page it stops if the worst case would pass the cap.
- Model: `DONKEY_CRAWL_MODEL` (default `claude-opus-5-5`, effort low; `claude-haiku-5-5` is about 40x cheaper per token).
- Public pages only: robots.txt is honoured, a page that refuses (401/403/429, a CAPTCHA or bot challenge) is skipped
  and listed, never worked around. Two seconds between pages, one retry on a network error or 5xx.
- Only public hosts are fetched (no localhost or private addresses). A page the model declines, times out on or
  garbles is listed as an error and the run goes on; a declined call still counts toward the cap.

## Lease calendar and two score boosts (v0.6.2, free)

`python -m donkey lease-calendar` lists copier leases likely ending from 30 days ago to 12 months ahead
(`--months N`), grouped by month, soonest first, with who decides. `--all-lessors` adds equipment-finance leases,
`--csv FILE` writes the rows, and `api.lease_calendar()` returns them. The end dates are estimates (filing + 60 months,
`donkey/ucc.py`), so read the month as when to call, not as a contract date. Only the estimated end decides the
window; `lease_window_start` (filing + 36 months) is shown for reference. A business with several filings from the same
lessor and end month shows once; terminated or lapsed filings are left out when the index gives a status.

call-this-week now adds two boosts to a business that is already on the list. A boost never puts a business on the
list by itself.
- `print_heavy` (+1, "heavy-print line of work"): law, accounting/tax, medical and dental, real estate, title and
  escrow, mortgage, insurance, schools and colleges; matched on the name or the stored category (Overture's
  `elementary_school` style categories count). Churches, "Academy Sports"-style names and bare "charter" don't.
- `hiring` (+2, "hiring or expanding"): the who-decides answer's initiative line mentions hiring, job posts, a new
  location or an expansion, and doesn't say "not hiring" or "no expansion". The who-decides prompt now also searches "NAME TOWN AZ jobs" and writes "hiring" when it
  sees a job post (same $5 cap).

## Crustdata headcount growth (v0.6.3)

Source map `crustdata-companies` reads a company CSV from Crustdata's Data Export (app.crustdata.com). Rows outside
Arizona are skipped; town comes from the headquarters column ("Casa Grande, Arizona, United States") and each row is
stored as a `headcount_growth` event dated the import day.

    python -m donkey ingest --source crustdata-companies --file FILE.csv --received-from "Crustdata export"

call-this-week then gives the `hiring` boost (+2, same signal as the who-decides one, never counted twice) to a listed
business whose headcount grew in the 6 months before the export, for 180 days after the import. The export has no
ZIP or street, so it matches on name and town.

## Phone and website match keys (v0.6.4)

Each business stores `phone_key` (10 digits; toll-free numbers are left out, chains share them) and `domain_key` (the
website host without `www.`; Facebook, Yelp, LinkedIn, Wix, GoDaddy and other shared hosts are left out). Maps that
carry them: `city-business-list` (Business Phone, Website; a plain "Phone" column stays the contact's, it can be
an owner's cell), `overture-places`, `overture-history-db`, `crustdata-companies`
(domain). Matching adds three rules on top of name + ZIP/street, and never merges otherwise:

- Same name, new address in the same town, same phone, and no other same-name business stored: it moved; matched,
  not stored twice. A shared domain alone never counts here (stores of a chain share one).
- Same-name tie (several in the ZIP, or a Crustdata row with several in its town): exactly one with the same phone
  or domain wins; otherwise the row stays `needs_review`.
- Different spelling ("Sample Title And Escrow" vs "Sample Title Agency"): matched only when phone AND domain agree
  with exactly one business whose name shares words with it and whose ZIP (when both are known) is the same.

Doctors or tenants sharing a building phone and website keep separate rows (their names share no words). A match
the keys decided adds no keys to the stored business. An older donkey.db gets the
columns the first time it is opened, with keys filled from the stored source rows read through today's maps (one transaction);
nothing to run. Matched rows don't change the stored address.

## City business licenses on the Monday list (v0.6.5)

Source map `city-licenses` is `city-business-list` plus an issue date: each row is also a `new_license` event dated
its Issue Date, and call-this-week scores it 3 points (like a formation notice) for 60 days. Cities reissue every
renewal on Jan 1 and the list has no new/renewal flag, so a license dated Jan 1 never scores (a real Jan 1 opening is missed, and a renewal issued on another
day still scores). Home businesses don't score either: a
license whose legal name is a person (no LLC, Inc, church, school...) at an apartment or a lane/court/drive-type street
(`triggers.looks_home_based`; Buck 2026-10-19 23:21 UTC). A file without an issue-date column is refused. Dates may be ISO or the city export's MM-DD-YYYY. A file already
ingested under another map (same bytes) is refused with a note naming that map.

    python -m donkey ingest --source city-licenses --file casa_grande_licenses_2026.csv --received-from "Casa Grande NextRequest 26-330"

First list: Casa Grande NextRequest #26-330 (2026-10-19), 1,176 licenses issued 2026-01-01 to 2026-10-01 (no 2025
rows), all in 85122/85193/85194; 41 issued in the 60 days before 2026-10-19. Commercial use declared under A.R.S.
39-121.03: the file stays out of the repo (project files data/casa_grande/).
