"""The `analyze_runs` table: one row per Analyze run (P6).

Why this is its own module and not part of `app/analyze.py`: everything in there
runs a model or drives a child process, and none of it should know that SQLite
exists. The split is the same one `app/alerts.py` makes -- the rules live in one
file, the SQL for their table lives in another -- and it keeps the one rule that
matters intact: **the write functions take a connection they did not open**, so
they run on the pipeline's single writer thread like every other write in the
app. Nothing here opens a write connection of its own.

The read functions do open a connection, and only for reads. SQLite in WAL mode
takes any number of concurrent readers; what it will not take is a second
writer, and there is not one here.

An Analyze run is not a sighting and this table never joins to `sightings`.
What it stores is the run -- what file, how it went, and where the saved result
document and thumbnail are. The detections stay in that document.
"""

from pathlib import Path

from app import config
from app.db import connect, utc_now

COLUMNS = (
    "run_id",
    "kind",
    "original_filename",
    "status",
    "progress",
    "error",
    "created_ts",
    "thumbnail_path",
    "result_path",
)

# The four the contract freezes. A run the user stopped is stored as `error`
# with a message saying so rather than as a fifth value -- see CANCELLED_ERROR.
STATUSES = ("queued", "processing", "done", "error")

CANCELLED_ERROR = (
    "Stopped before it finished, so there is no result to show. "
    "Analyze the file again to read all of it."
)

INTERRUPTED_ERROR = (
    "The app was stopped while this was running, so it never finished. "
    "Analyze the file again."
)

# Fields a caller may update. `run_id`, `kind` and `created_ts` are what the row
# is; the rest is how it went.
UPDATABLE = (
    "status", "progress", "error", "thumbnail_path", "result_path",
    "original_filename",
)


def _row(row):
    return None if row is None else dict(row)


def store_path(path):
    """A path as it is written into the table.

    Relative to the project root when it is under it, which is what
    `sightings.crop_path` already does, so a row moved between machines is not
    carrying somebody's drive letter.
    """
    if path is None:
        return None
    path = Path(path)
    try:
        return path.resolve().relative_to(config.ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def full_path(stored):
    """A stored path back to something that can be opened."""
    if not stored:
        return None
    path = Path(stored)
    return path if path.is_absolute() else config.ROOT / path


# ------------------------------------------------------------------- writes
# Every one of these takes the writer's connection. None of them opens one.


def insert(conn, kind, original_filename, status="queued", progress=None):
    cursor = conn.execute(
        "INSERT INTO analyze_runs (kind, original_filename, status, progress, "
        "created_ts) VALUES (?, ?, ?, ?, ?)",
        (kind, original_filename, status, progress, utc_now()),
    )
    conn.commit()
    return _row(
        conn.execute(
            "SELECT * FROM analyze_runs WHERE run_id = ?", (cursor.lastrowid,)
        ).fetchone()
    )


def update(conn, run_id, **fields):
    """Change some columns of one run. Unknown names are refused, not ignored."""
    unknown = [k for k in fields if k not in UPDATABLE]
    if unknown:
        raise ValueError(f"analyze_runs has no updatable column {unknown[0]!r}")
    if not fields:
        return get(conn, run_id)
    assignments = ", ".join(f"{name} = ?" for name in fields)
    conn.execute(
        f"UPDATE analyze_runs SET {assignments} WHERE run_id = ?",
        (*fields.values(), int(run_id)),
    )
    conn.commit()
    return get(conn, run_id)


def remove(conn, run_id):
    cursor = conn.execute(
        "DELETE FROM analyze_runs WHERE run_id = ?", (int(run_id),)
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_interrupted(conn):
    """Every run left mid-flight by a previous process, failed with the reason.

    Called once at startup. A `queued` or `processing` row can only mean the
    process that owned it is gone -- there is no other way for one to survive a
    restart -- and a progress bar that never moves again is worse than a row
    that says what happened. The row itself is kept: the user deletes it, not
    the app.
    """
    rows = conn.execute(
        "SELECT * FROM analyze_runs WHERE status IN ('queued', 'processing')"
    ).fetchall()
    if not rows:
        return []
    conn.execute(
        "UPDATE analyze_runs SET status = 'error', error = ?, progress = NULL "
        "WHERE status IN ('queued', 'processing')",
        (INTERRUPTED_ERROR,),
    )
    conn.commit()
    return [dict(row) for row in rows]


def evict(conn, keep, protect=()):
    """Drop the oldest finished runs past `keep`, and return them.

    Only finished ones: a queued or running run is not old, it is happening.
    The caller deletes their directories -- this decides which, because the
    decision is a query and the deletion is a filesystem problem.
    """
    protect = {int(r) for r in protect}
    rows = conn.execute(
        "SELECT * FROM analyze_runs ORDER BY run_id DESC"
    ).fetchall()
    doomed = []
    kept = 0
    for row in rows:
        if kept < keep or row["status"] not in ("done", "error"):
            kept += 1
            continue
        if int(row["run_id"]) in protect:
            kept += 1
            continue
        doomed.append(dict(row))
    for row in doomed:
        conn.execute(
            "DELETE FROM analyze_runs WHERE run_id = ?", (row["run_id"],)
        )
    if doomed:
        conn.commit()
    return doomed


def get(conn, run_id):
    try:
        key = int(run_id)
    except (TypeError, ValueError):
        return None
    return _row(
        conn.execute(
            "SELECT * FROM analyze_runs WHERE run_id = ?", (key,)
        ).fetchone()
    )


def write_directly(fn):
    """Run one write with a connection opened here.

    Only for an `AnalysisJobs` with no writer bound to it -- a route test or a
    script that built one by hand. The running app always binds the pipeline's
    writer, so this is never the path that touches the application database
    while workers are streaming.
    """
    conn = connect()
    try:
        return fn(conn)
    finally:
        conn.close()


# -------------------------------------------------------------------- reads


def read_one(run_id):
    conn = connect()
    try:
        return get(conn, run_id)
    finally:
        conn.close()


def read_recent(limit=50):
    """Newest first, which is the order the rail shows them in."""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM analyze_runs ORDER BY run_id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def read_ids():
    """Every run id in the table, as strings -- the directory names to keep."""
    conn = connect()
    try:
        rows = conn.execute("SELECT run_id FROM analyze_runs").fetchall()
    finally:
        conn.close()
    return {str(row["run_id"]) for row in rows}
