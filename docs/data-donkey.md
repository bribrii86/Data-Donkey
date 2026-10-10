# Data Donkey

Data Donkey is the shared data store for acquired business lists, UCC filings,
Overture places and leads. It is a standalone `donkey` Python package and SQLite
database, separate from the Stephanie sales app.

## Layout and database

- `donkey/` contains the schema, ingestion CLI, read-only query API and source maps.
- `donkey/sources/*.json` maps each source's input columns to common fields.
- `donkey.db` lives at the repository root by default. Set `DONKEY_DB` to use a
  different file. The database is ignored by Git.
- `samples/city-business-list.csv` is synthetic data for a city-list example.

The SQLite schema has six tables:

- `sources`: source identifiers and descriptions.
- `batches`: each ingested file, SHA-256, receipt time and sender, plus source row count.
- `businesses`: normalized business identities and searchable location/vertical details.
- `records`: every original source row as JSON, its batch, match status and optional business link.
- `events`: dated business events such as `ucc_filing`.
- `contacts`: business contacts carried by imported source rows.

Identical file contents (the same SHA-256) are only ingested once, even if the
file name changes. Names are normalized with the Stephanie app's existing
`prospects._norm`. A row is matched only when normalized names and ZIP or street
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

## The Stephanie app reads Data Donkey (v0.4.1)

The app gets UCC filings from `prospects.load_ucc_filings()`, in this order:

1. `STEPHANIE_FAKE_SOURCES=1`: `samples/pilot/ucc_fake.csv`, exactly as before.
2. `donkey.db` (`DONKEY_DB`, else the repo root) when it has `ucc_filing` events: the debtor is the business
   name, the secured party and filing number come from the event details, the filing date from `event_date`.
3. Otherwise `data/ucc.csv` (or `STEPHANIE_UCC_CSV`) as before.

A UCC file uploaded on the Prospects page, or an explicit path, still overrides all of this. With no `donkey.db`
the app gives the same results as v0.4.0.

## Copy the app's Overture places

```console
python -m donkey ingest-overture --history-db PATH
```

Copies the `overture_places` table from `history.db` (default: `STEPHANIE_DB`, else the repo root) into the
`overture-history-db` source. The batch records the sha256 of the table contents; the same contents are never
ingested twice, and every row is kept in `records` as JSON.

## Doctor

`python -m stephanie_assistant.cli doctor` prints the `donkey.db` path, row counts per source and when the newest
batch was received. A missing `donkey.db` is a WARN, not a FAIL.
