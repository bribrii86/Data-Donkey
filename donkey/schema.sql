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
    review_candidates_json TEXT NOT NULL DEFAULT '[]',
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

-- Growth tracking (v0.5.1): dated copies of a business list; growth-diff turns changes into growth_* events.
CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY,
    label TEXT NOT NULL,
    taken_at TEXT NOT NULL,
    file_name TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    row_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS snapshot_rows (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    key TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    street TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    zip TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    home_based INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (snapshot_id, key)
);

-- v0.5.3: one row per weekly growth runner attempt (paid Console agent calls). The monthly cap sums cost_cents,
-- or budget_cents while a run has no final cost.
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    area TEXT NOT NULL,
    town TEXT NOT NULL,
    session_id TEXT,
    status TEXT NOT NULL,
    budget_cents INTEGER,
    cost_cents INTEGER,
    files INTEGER,
    rows INTEGER,
    snapshot_id INTEGER,
    changes INTEGER,
    note TEXT
);

-- v0.6.0: who decides. One row per business (normalized name + ZIP, else + town): can it buy locally? Filled by the
-- free rules (locality.classify), the NPI registry, the weekly Console agent, or by hand. details_json keeps the raw
-- evidence (NPI records, the agent's row) so nothing learned is thrown away.
CREATE TABLE IF NOT EXISTS locality (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    zip TEXT NOT NULL DEFAULT '',
    town TEXT NOT NULL DEFAULT '',
    value TEXT NOT NULL CHECK (value IN ('local', 'franchise', 'regional', 'corporate', 'central_procurement',
                                         'unknown')),
    evidence TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'low' CHECK (confidence IN ('high', 'med', 'low')),
    owner_hint TEXT NOT NULL DEFAULT '',
    initiative TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}'
);

-- v0.6.0: Overture's brand field per place (13.7% of places in Stephanie's ZIPs carried one in release 2026-09-23.1).
CREATE TABLE IF NOT EXISTS place_brands (
    key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    zip TEXT NOT NULL DEFAULT '',
    brand TEXT NOT NULL,
    wikidata TEXT NOT NULL DEFAULT '',
    release TEXT NOT NULL DEFAULT ''
);

-- v0.6.0: chain and franchise brand names imported from open lists (OpenStreetMap Name Suggestion Index).
CREATE TABLE IF NOT EXISTS brands (
    normalized_name TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'chain',
    wikidata TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL
);
