"""Settings loader.

Paths resolve against the project root, never the cwd. Worker processes spawned
in P1 do not inherit the cwd of whoever started the app, and a relative path
that works from the repo root fails silently from anywhere else.
"""

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

_settings = None


def _resolve(value):
    """Make a configured path absolute against the project root.

    None passes through untouched: a null model entry means "not trained yet",
    not "missing setting".
    """
    if value is None:
        return None
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def load_settings(reload=False):
    """Read config/settings.yaml once and cache it."""
    global _settings
    if _settings is not None and not reload:
        return _settings

    path = CONFIG_DIR / "settings.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"No settings file at {path}. The app cannot start without it."
        )
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    raw["models"] = {k: _resolve(v) for k, v in (raw.get("models") or {}).items()}
    raw["paths"] = {k: _resolve(v) for k, v in (raw.get("paths") or {}).items()}
    raw.setdefault("server", {})
    raw.setdefault("defaults", {})

    _settings = raw
    return _settings


def model_path(name):
    """Weights path for a configured model, or None if it is not trained yet."""
    return load_settings()["models"].get(name)


def db_path():
    return load_settings()["paths"]["db"]


def crops_dir():
    return load_settings()["paths"]["crops"]


def plate_crops_dir():
    return load_settings()["paths"]["plate_crops"]


def unsorted_crops_dir():
    return load_settings()["paths"]["unsorted_crops"]


def uploads_dir():
    """Where a video or image uploaded from the UI lands.

    Inside footage/ on purpose: an uploaded clip is footage like any other, and
    a source that points at one is the same kind of row as a seeded source.
    """
    return load_settings()["paths"].get("uploads") or (ROOT / "footage" / "uploads")


def analyze_dir():
    """Where one Analyze job's annotated frames, crops and result document go.

    Deliberately not under crops/: everything in there is a sighting's evidence
    and nothing an Analyze job produces is a sighting. A job directory is
    deleted with its job.
    """
    return load_settings()["paths"].get("analyze") or (ROOT / "data" / "analyze")


def sources_seed_path():
    return CONFIG_DIR / "sources.yaml"


def blacklist_path():
    """The plates alerts are raised on (P5).

    Config rather than runtime state, and re-read on every sighting when its
    mtime changes, so adding a registration takes effect within seconds with
    nothing to restart. Overridable through `paths.blacklist` so a test can
    point at a throwaway file instead of the shipped one.
    """
    configured = load_settings()["paths"].get("blacklist")
    return configured if configured is not None else CONFIG_DIR / "blacklist.yaml"


def default(key, fallback=None):
    return load_settings()["defaults"].get(key, fallback)


def ensure_dirs():
    """Create the directories the app writes into."""
    settings = load_settings()
    db_path().parent.mkdir(parents=True, exist_ok=True)
    uploads_dir().mkdir(parents=True, exist_ok=True)
    analyze_dir().mkdir(parents=True, exist_ok=True)
    for key in ("crops", "plate_crops", "unsorted_crops"):
        directory = settings["paths"].get(key)
        if directory is not None:
            directory.mkdir(parents=True, exist_ok=True)
