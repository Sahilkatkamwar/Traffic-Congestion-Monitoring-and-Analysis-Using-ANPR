"""SQLite schema, connections, and the timestamp format.

The schema below is the frozen data contract in CLAUDE.md. Do not add or rename
fields here without asking.

SQLite has no timestamp or json type, so:
  - timestamps are ISO-8601 UTC text, e.g. '2026-08-31T09:00:00.000Z'. They sort
    lexicographically and serialise straight to the UI without conversion.
  - json columns hold json.dumps output as text.

One writer. Workers push to a queue and a single process drains it (P1).
Concurrent writers lock even in WAL mode.
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.config import db_path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    source_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    uri         TEXT NOT NULL,
    lat         REAL,
    lon         REAL,
    heading_deg INTEGER,
    fps         REAL,
    frame_skip  INTEGER NOT NULL DEFAULT 3,
    start_time  TEXT,
    status      TEXT NOT NULL DEFAULT 'idle',
    error       TEXT,
    progress    REAL
);

-- One row per vehicle track, never per frame.
--
-- No UNIQUE(source_id, track_id): track ids are unique within a single worker
-- run, so re-processing a file restarts them at 1 and a unique constraint would
-- silently reject the whole second run.
--
-- Every plate column is nullable. A sighting with no plate read is valid and
-- still written.
CREATE TABLE IF NOT EXISTS sightings (
    sighting_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id        TEXT NOT NULL REFERENCES sources(source_id),
    track_id         INTEGER NOT NULL,
    plate_raw        TEXT,
    plate_text       TEXT,
    plate_conf       REAL,
    plate_candidates TEXT,
    vehicle_type     TEXT,
    vehicle_color    TEXT,
    first_seen_ts    TEXT,
    last_seen_ts     TEXT,
    crop_path        TEXT,
    plate_crop_path  TEXT,
    frames_voted     INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL,
    severity     TEXT NOT NULL,
    plate_text   TEXT,
    sighting_ids TEXT,
    detail       TEXT,
    created_ts   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sightings_plate    ON sightings (plate_text);
CREATE INDEX IF NOT EXISTS idx_sightings_first    ON sightings (first_seen_ts);
CREATE INDEX IF NOT EXISTS idx_sightings_src_time ON sightings (source_id, first_seen_ts);
CREATE INDEX IF NOT EXISTS idx_alerts_created     ON alerts (created_ts);
"""


def to_iso(dt):
    """Format a datetime as ISO-8601 UTC text. Naive input is assumed UTC."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def from_iso(text):
    """Parse stored timestamp text back to an aware datetime."""
    if text is None:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def utc_now():
    return to_iso(datetime.now(timezone.utc))


def connect(path=None):
    """Open a fresh connection.

    Never share one across threads or processes: FastAPI runs sync handlers on a
    threadpool, and a connection cannot cross a spawn boundary at all.
    """
    target = Path(path) if path is not None else db_path()
    conn = sqlite3.connect(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_db(path=None):
    """Create the database and schema if they do not exist. Idempotent."""
    target = Path(path) if path is not None else db_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(target)
    try:
        # WAL is written into the file header, so this sticks for every later
        # connection.
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            print(f"[db] WARNING: journal_mode is {mode!r}, not WAL")
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()

    print(f"[db] ready at {target}")
    return target
