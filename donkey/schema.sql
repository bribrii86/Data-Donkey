CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    received_at TEXT NOT NULL,
    received_from TEXT NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    vertical TEXT NOT NULL DEFAULT '',
    license_number TEXT NOT NULL DEFAULT '',
    open_date TEXT NOT NULL DEFAULT '',
    naics TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_businesses_city_vertical
    ON businesses(city, vertical);
CREATE INDEX IF NOT EXISTS idx_businesses_name
    ON businesses(normalized_name);

CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL REFERENCES sources(id),
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    row_number INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    business_id INTEGER REFERENCES businesses(id),
    match_status TEXT NOT NULL CHECK (match_status IN ('new', 'matched', 'needs_review')),
    UNIQUE(batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    business_id INTEGER REFERENCES businesses(id),
    record_id INTEGER NOT NULL REFERENCES records(id),
    kind TEXT NOT NULL,
    event_date TEXT NOT NULL DEFAULT '',
    town TEXT NOT NULL DEFAULT '',
    details_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_kind_date ON events(kind, event_date);
CREATE INDEX IF NOT EXISTS idx_events_town ON events(town);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    business_id INTEGER REFERENCES businesses(id),
    record_id INTEGER NOT NULL REFERENCES records(id),
    name TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT ''
);
