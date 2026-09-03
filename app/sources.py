"""What a source is: its uri, its kind, and whether a submitted one is usable.

P1 kept uri resolution inside worker.py because the worker was the only thing
that ever turned a source into a capture. P4b adds two more makers -- the UI,
and a connection test that runs in its own process -- and all three have to
agree on what `0` or `footage/clips/20 sec.mp4` means. So the rule lives here
and worker.py imports it.

Nothing in this module loads a model, opens a capture, or touches the database.
It is imported by a subprocess whose entire job is to open one capture and
exit, so it must stay cheap.
"""

import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from app import config

KINDS = ("file", "webcam", "network", "image")

VIDEO_SUFFIXES = {".mp4", ".mov", ".avi", ".mkv", ".m4v", ".webm", ".mpg", ".mpeg"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}

LAT_RANGE = (-90.0, 90.0)
LON_RANGE = (-180.0, 180.0)


def resolve_uri(uri):
    """A uri string becomes a webcam index, a URL, or an absolute file path.

    This is the only place the shape of a source is inspected, and it produces
    a capture argument -- not a branch anyone downstream can see.
    """
    text = str(uri).strip()
    if text.isdigit():
        return int(text)
    if "://" in text:
        return text
    path = Path(text)
    return str(path if path.is_absolute() else config.ROOT / path)


def kind_for_uri(uri):
    """The kind a uri looks like.

    A label on the record and a hint for the UI, never a branch downstream. The
    worker is not given `kind` and does not behave differently for any value.
    """
    text = str(uri).strip()
    if text.isdigit():
        return "webcam"
    if "://" in text:
        return "network"
    if Path(text).suffix.lower() in IMAGE_SUFFIXES:
        return "image"
    return "file"


def slug(text, fallback="source"):
    """A source_id from a name: ascii, lowercase, underscores, never empty."""
    normalised = unicodedata.normalize("NFKD", str(text or ""))
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", ascii_only).strip("_").lower()
    return (cleaned or fallback)[:40]


def unique_id(base, taken):
    """`base`, or base_2, base_3 ... until it is not already a source_id."""
    if base not in taken:
        return base
    for n in range(2, 1000):
        candidate = f"{base}_{n}"
        if candidate not in taken:
            return candidate
    raise ValueError(f"Could not find a free source id starting with {base}.")


def safe_filename(name):
    """An uploaded filename reduced to something Windows will accept.

    Path components are dropped, not sanitised: a browser is allowed to send
    'C:\\Users\\x\\clip.mp4' or '../../evil.mp4', and only the last segment is
    ever a filename.
    """
    tail = str(name or "").replace("\\", "/").split("/")[-1]
    tail = unicodedata.normalize("NFKD", tail).encode("ascii", "ignore").decode()
    tail = re.sub(r'[<>:"|?*]', "", tail)
    tail = "".join(ch for ch in tail if ch >= " ").strip(" .")
    return tail or "upload"


def parse_time(value, field="start_time"):
    """Accept what an <input type=datetime-local> sends, plus stored ISO text.

    A datetime-local field carries no timezone and the browser means local time
    by it. Anything without an offset is read as local and stored as UTC --
    stamping local wall-clock text as UTC would shift every trajectory by the
    machine's offset.
    """
    if value in (None, ""):
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(
            f"start_time is not a time I can read: {value}. "
            f"Expected something like 2026-08-31T09:00 or 2026-08-31T09:00:00Z."
        )
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()  # local time, as the browser meant it
    return parsed.astimezone(timezone.utc)


def to_iso(dt):
    if dt is None:
        return None
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _number(value, field, low, high):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a number, not {value}.")
    if not low <= number <= high:
        raise ValueError(f"{field} must be between {low} and {high}, not {number}.")
    return number


def local_path(uri):
    """The absolute path a file/image uri points at, or None if it is not one."""
    target = resolve_uri(uri)
    if isinstance(target, int) or "://" in str(target):
        return None
    return Path(target)


def check_reachable(kind, uri):
    """A file that is not there is worth saying so about before it is saved.

    Only for paths. A camera or a URL is checked by opening it -- that is what
    the connection test is for -- and guessing about one here would be wrong.
    """
    if kind not in ("file", "image"):
        return
    path = local_path(uri)
    if path is not None and not path.exists():
        raise ValueError(
            f"There is no file at {path}. Upload it, or pick one from the list."
        )


def clean(payload, taken_ids=(), existing=None):
    """Validate a submitted source into a row, or raise ValueError with a reason.

    `existing` is the current row when this is an edit; fields the payload does
    not mention are left alone. The messages are written for a person, because
    a person is who is shown them.
    """
    editing = existing is not None
    row = dict(existing) if editing else {}

    if not editing or "name" in payload:
        name = str(payload.get("name") or "").strip()
        if not name:
            raise ValueError(
                "Give the source a name. It is how it is labelled everywhere else."
            )
        row["name"] = name[:120]

    if not editing or "uri" in payload:
        uri = payload.get("uri")
        uri = "" if uri is None else str(uri).strip()
        if not uri:
            raise ValueError(
                "A source needs a uri: a webcam index, a stream URL, or a file path."
            )
        row["uri"] = uri

    if not editing or "kind" in payload:
        kind = str(payload.get("kind") or "").strip().lower()
        if not kind:
            kind = kind_for_uri(row["uri"])
        if kind not in KINDS:
            raise ValueError(
                f"kind must be one of {', '.join(KINDS)}, not {kind}."
            )
        row["kind"] = kind

    check_reachable(row["kind"], row["uri"])

    for field, bounds in (("lat", LAT_RANGE), ("lon", LON_RANGE)):
        if not editing or field in payload:
            row[field] = _number(payload.get(field), field, *bounds)
    # A trajectory needs both halves of a coordinate. One without the other is
    # not a partial placement, it is a broken one.
    if (row.get("lat") is None) != (row.get("lon") is None):
        raise ValueError(
            "Place the source on the map or leave it unplaced -- "
            "lat and lon travel together."
        )

    if not editing or "heading_deg" in payload:
        heading = _number(payload.get("heading_deg"), "heading_deg", 0, 359)
        row["heading_deg"] = None if heading is None else int(heading)

    if not editing or "frame_skip" in payload:
        raw = payload.get("frame_skip")
        if raw in (None, ""):
            row["frame_skip"] = int(config.default("frame_skip", 3))
        else:
            try:
                skip = int(raw)
            except (TypeError, ValueError):
                raise ValueError(f"frame_skip must be a whole number, not {raw}.")
            if not 1 <= skip <= 60:
                raise ValueError(
                    "frame_skip must be between 1 and 60. Above that the tracker "
                    "loses vehicles between the frames it is shown."
                )
            row["frame_skip"] = skip

    if not editing or "start_time" in payload:
        when = parse_time(payload.get("start_time"))
        # A live source is stamped from the wall clock at capture, so a
        # start_time on one is a value that can never be used. Dropping it is
        # honest; keeping it invites someone to trust it later.
        if row["kind"] in ("webcam", "network"):
            when = None
        row["start_time"] = to_iso(when)

    if not editing:
        wanted = str(payload.get("source_id") or "").strip()
        base = slug(wanted or row["name"], fallback="source")
        if wanted and base != wanted.lower():
            raise ValueError(
                f"source_id can only hold letters, digits and underscores. "
                f"{wanted} would become {base} -- use that, or leave it blank."
            )
        if wanted and base in set(taken_ids):
            raise ValueError(f"There is already a source called {base}.")
        row["source_id"] = unique_id(base, set(taken_ids))
        row["fps"] = _number(payload.get("fps"), "fps", 0.1, 240)
        row["status"] = "idle"
        row["error"] = None
        row["progress"] = None

    return row
