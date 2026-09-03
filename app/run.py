"""Entrypoint:  python -m app.run

The __main__ guard at the bottom is mandatory. Windows multiprocessing uses
spawn, which re-imports this module in every child; without the guard the
worker processes added in P1 respawn forever.
"""

import yaml
import uvicorn

from app import config, db
from app.api import create_app
from app.pipeline import Pipeline

SEED_COLUMNS = (
    "source_id",
    "name",
    "kind",
    "uri",
    "lat",
    "lon",
    "heading_deg",
    "fps",
    "frame_skip",
    "start_time",
)
REQUIRED = ("source_id", "name", "kind", "uri")


def _missing(entry, key):
    """A field counts as present unless it is absent, null, or blank.

    Not falsiness: webcam index 0 is a perfectly good uri.
    """
    value = entry.get(key)
    return value is None or (isinstance(value, str) and not value.strip())


def _seed_row(entry):
    """Turn one YAML entry into a row, or return None if it is unusable."""
    missing = [k for k in REQUIRED if _missing(entry, k)]
    if missing:
        print(
            f"[seed] skipping entry {entry.get('source_id') or '<no source_id>'}: "
            f"missing {', '.join(missing)}"
        )
        return None

    row = {k: entry.get(k) for k in SEED_COLUMNS}
    row["uri"] = str(row["uri"])
    frame_skip = entry.get("frame_skip")
    row["frame_skip"] = config.default("frame_skip", 3) if frame_skip is None else frame_skip
    # PyYAML turns an unquoted timestamp into a datetime, which sqlite3 will not
    # bind. Normalise everything to the stored ISO-8601 UTC text.
    start = row["start_time"]
    row["start_time"] = db.to_iso(start) if hasattr(start, "tzinfo") else start
    return row


def seed_sources():
    """Load config/sources.yaml, but only on a first run.

    The file is a seed. The moment the app owns a sources table, sources are
    runtime state managed from the UI, and re-reading the seed would resurrect
    anything the user deleted.
    """
    path = config.sources_seed_path()
    if not path.exists():
        return 0

    with path.open(encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    entries = doc.get("sources") or []
    if not entries:
        return 0

    conn = db.connect()
    try:
        if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] > 0:
            return 0
        rows = [r for r in (_seed_row(e) for e in entries) if r is not None]
        if not rows:
            return 0
        conn.executemany(
            f"INSERT INTO sources ({', '.join(SEED_COLUMNS)}, status) "
            f"VALUES ({', '.join(':' + c for c in SEED_COLUMNS)}, 'idle')",
            rows,
        )
        conn.commit()
    finally:
        conn.close()

    print(f"[seed] inserted {len(rows)} source(s) from {path.name}")
    return len(rows)


def main():
    settings = config.load_settings()
    config.ensure_dirs()
    db.init_db()
    seed_sources()

    # The pipeline starts and stops with the server: it owns the worker
    # processes, and orphaned workers would keep holding cameras and files.
    pipeline = Pipeline()

    host = settings["server"].get("host", "127.0.0.1")
    port = int(settings["server"].get("port", 8000))
    print(f"[run] serving on http://{host}:{port}  health: /api/health")

    # Pass the app object, not an import string, and never reload=True: the
    # reloader forks a second process that re-imports and re-initialises
    # everything.
    uvicorn.run(
        create_app(pipeline), host=host, port=port, reload=False, log_level="info"
    )


if __name__ == "__main__":
    main()
