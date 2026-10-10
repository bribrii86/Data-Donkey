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
agree. Ambiguous or insufficiently located same-name rows are retained in
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
- Rows match across snapshots on phone + name, else name + ZIP.
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
months). Arizona records no collateral, so these are estimates from the lender's name.

**Call this week.** `donkey.api.call_this_week(area, as_of, days=14, min_score=3, scorers=())` ranks businesses by
adding points from every trigger in the window: growth events (above), a new filing with a copier lessor (4),
equipment finance (3), bank or other (1), and a copier lease whose estimated end is within 180 days (5). Each row has
`score`, `signals` and plain-language `reasons`. Apps add their own triggers by passing scorer functions (for
example a permit feed); hits for the same business name and ZIP are added together. Scores live in
`donkey.triggers.SCORES`.

```console
python -m donkey call-this-week --area stephanie
```
