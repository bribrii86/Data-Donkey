# Data Donkey

Data Donkey stores the data Brian Parris acquires (city business lists, UCC filings, Overture places, leads) in one
SQLite file, so any app can read it. The Stephanie sales app (repo Sales-Pilot) is the first reader.

- Package: `donkey/` (standard library only). Docs: `docs/data-donkey.md`.
- Database: `~/DataDonkey/donkey.db` (set `DONKEY_DB` to move it). Never committed.
- Ingest: `python -m donkey ingest --source SOURCE_ID --file FILE_PATH --received-from NAME`
- Read: `donkey.api.search`, `events`, `lookup`, `needs_review`.

## Develop

```console
python -m venv .venv
.venv\Scripts\pip install -e ".[dev]"
.venv\Scripts\python -m pytest -q
.venv\Scripts\ruff check .
.venv\Scripts\bandit -q -r donkey
.venv\Scripts\pip-audit
```

## Moving an older donkey.db

Before v0.5.0 the file lived in the app folder (for example `C:\AIProjects\Data-Donkey\donkey.db`). Move it to
`C:\Users\NAME\DataDonkey\donkey.db`, or set `DONKEY_DB` to its path.
