"""Two things worth interrupting someone about, decided as the row is written.

**A blacklisted plate.** A list of registrations somebody cares about, matched
against every sighting the moment it commits. Never by string equality: the
camera read that vehicle as MH15HY22 and the blacklist says MH15HY2237, and an
equality test answers "not seen" about a vehicle sitting in the database.

**An impossible transition.** One vehicle at two cameras, and the distance
between them divided by the time between them is a speed no road produces. That
does not mean the vehicle teleported -- it means one of the two plate reads
belongs to a different vehicle, or the plate is cloned. Either is worth showing,
and the alert shows the arithmetic rather than the conclusion.

Both run on the writer thread, inside the commit path, because the exit
criterion is measured in seconds: a blacklisted plate has to raise an alert as
its sighting lands, not on the next poll. Nothing here may raise into the
writer -- the writer is the only thing that writes to SQLite, and losing an
alert is survivable where stopping every source is not.

## The matching gate, and why it is a distance rather than a score

`matching.similarity` normalises by length, so the same tolerance means one
thing on an eight-character plate and another on an eleven-character one. What
this decision needs is length-independent: *how many characters had to change,
and were they characters OCR actually confuses.* That is the raw
confusion-weighted distance, where a confusable swap costs 0.35 and a real one
costs 1.0.

Measured over the 43 distinct plate strings in the application database:

    HR15B1238 / HR15B1738   1.00   one vehicle, read twice
    HR03A7979 / HR03X7979   1.00   one vehicle, read twice
    HR15B1738 / MP15B1738   1.35   TWO vehicles -- different state code

So 1.0 is the floor that takes both genuine re-reads and refuses the pair that
differs by a state code. Expressed as similarity the same three pairs are
0.889, 0.889 and 0.850, which is a band of 0.039 to sit a threshold in -- the
distance form is the same measurement with the length dependence removed.

A blacklist entry is a string a person typed, so only one side of that
comparison is noisy. Two OCR reads scored against each other are noisy on both
sides and could justify a wider gate; they share this one instead, erring
towards refusing a match. A missed alert is a missed alert. A wrong one puts
the wrong vehicle in front of whoever reads it.

## Severity is not a decoration

`critical` is reserved for a hit that needs no interpretation: the read is
character-for-character the blacklisted plate, or a vehicle is at two separated
places at one instant, which no speed explains. Everything reached by fuzzy
matching is capped at `warning` and says how far off it was, because the person
reading it has to be able to disagree with it.
"""

import json
from datetime import timedelta
from pathlib import Path

import yaml

from app import config
from app.db import from_iso, to_iso, utc_now
from app.grammar import normalize
from app.matching import distance, plate_forms
from app.trajectory import haversine_km

# Confusion-weighted edit cost allowed between a stored read and the plate it is
# being matched to. See the measurement in the module docstring.
MATCH_DISTANCE = 1.0

# Above this between two cameras, the journey did not happen. 200 km/h is well
# clear of anything an Indian road produces and well under the four-figure
# numbers a mismatched pair of reads generates, so the alert fires on the
# arithmetic being absurd rather than on the vehicle being quick.
MAX_SPEED_KMH = 200.0

# Two cameras closer together than this are watching the same place. A vehicle
# crossing 30 m in the second between two frames is 108 km/h and is not
# evidence of anything.
MIN_DISTANCE_KM = 0.05

# How far back a transition is looked for. A pair of sightings a day apart
# cannot be an impossible transition however far apart the cameras are, and
# scanning the whole table on every committed row would put a growing cost
# inside the writer.
TRANSITION_WINDOW_SEC = 3600.0

SEVERITIES = ("info", "warning", "critical")
_RANK = {name: i for i, name in enumerate(SEVERITIES)}


def _setting(key, fallback):
    value = config.default(key, None)
    return fallback if value is None else value


def _cap(severity, ceiling):
    """The lower of two severities. A fuzzy hit cannot be critical."""
    if _RANK.get(severity, 2) <= _RANK[ceiling]:
        return severity
    return ceiling


# ------------------------------------------------------------------ blacklist


class Blacklist:
    """The watched plates, re-read from disk whenever the file changes.

    Reloading on mtime rather than at startup is the whole reason a blacklist
    can be a file at all: adding a registration takes effect on the next
    sighting, with nothing to restart. The check costs one stat() per sighting.

    A file that does not exist is not an error -- it is an empty blacklist,
    which is the shipped state. A file that does not parse IS an error, and it
    is kept and reported rather than swallowed, because a blacklist that
    silently became empty is worse than one that says it is broken.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path is not None else config.blacklist_path()
        self.entries = []
        self.skipped = []       # what could not be used, and why
        self.error = None       # the file itself failed to parse
        self.loaded_ts = None
        self._mtime = None
        self._loaded = False

    # -- loading ------------------------------------------------------------

    def _stamp(self):
        try:
            stat = self.path.stat()
        except OSError:
            return None
        # Size as well as mtime: two saves inside one filesystem timestamp tick
        # are a real thing on Windows, and a blacklist edit that does not take
        # effect is the failure this whole class exists to avoid.
        return (stat.st_mtime_ns, stat.st_size)

    def refresh(self, force=False):
        """Reload if the file changed. Returns True if it was re-read."""
        stamp = self._stamp()
        if self._loaded and not force and stamp == self._mtime:
            return False
        self._mtime = stamp
        self._loaded = True
        self._read()
        return True

    def _read(self):
        self.entries = []
        self.skipped = []
        self.error = None
        self.loaded_ts = utc_now()

        if self._mtime is None:
            return

        try:
            with self.path.open(encoding="utf-8") as handle:
                raw = yaml.safe_load(handle) or {}
        except (OSError, yaml.YAMLError) as exc:
            self.error = (
                f"{self.path.name} could not be read: {exc}. Fix the file and "
                f"save it -- it is re-read automatically, with no restart."
            )
            print(f"[alerts] {self.error}")
            return

        if isinstance(raw, list):
            listed = raw
        elif isinstance(raw, dict):
            listed = raw.get("plates")
            if listed is None:
                listed = []
        else:
            self.error = (
                f"{self.path.name} should hold a `plates:` list. Found "
                f"{type(raw).__name__}."
            )
            return
        if not isinstance(listed, list):
            self.error = (
                f"`plates:` in {self.path.name} should be a list of "
                f"registrations, one per line."
            )
            return

        seen = set()
        for item in listed:
            entry, reason = self._entry(item)
            if entry is None:
                self.skipped.append({"entry": _short(item), "reason": reason})
                continue
            if entry["plate"] in seen:
                self.skipped.append(
                    {"entry": entry["plate"], "reason": "listed more than once"}
                )
                continue
            seen.add(entry["plate"])
            self.entries.append(entry)

        note = f"[alerts] blacklist: {len(self.entries)} plate(s) from {self.path}"
        if self.skipped:
            note += f", {len(self.skipped)} line(s) skipped"
        print(note)

    @staticmethod
    def _entry(item):
        if isinstance(item, str):
            item = {"plate": item}
        if not isinstance(item, dict):
            return None, "not a plate string or a mapping with a `plate:` key"

        plate = normalize(str(item.get("plate") or ""))
        if not plate:
            return None, "no `plate:` value"
        if len(plate) < 4:
            return None, f"{plate} is too short to be a registration"

        severity = str(item.get("severity") or "critical").lower()
        if severity not in SEVERITIES:
            # Named by its plate rather than by the repr of the mapping it came
            # from: this reason is shown on the Alerts screen, and a user
            # reading which of their lines was skipped should see the
            # registration, not a dict.
            return None, (
                f"{plate}: severity {severity} is not one of "
                f"{', '.join(SEVERITIES)}"
            )
        reason = item.get("reason")
        return {
            "plate": plate,
            "severity": severity,
            "reason": None if reason is None else str(reason),
        }, None

    # -- matching -----------------------------------------------------------

    def match(self, sighting):
        """The best blacklist hit on one sighting, or None.

        Every string the sighting offers is tried -- the voted plate, the raw
        read, and the stored top-k alternatives -- because a plate whose primary
        read lost by one character is often correct in second place. Which one
        matched travels back with the hit, and the UI shows it: a match reached
        through a third-choice alternative must not look like an exact hit on
        the voted plate.
        """
        self.refresh()
        if not self.entries:
            return None

        limit = float(_setting("alert_match_distance", MATCH_DISTANCE))
        forms = plate_forms(sighting)
        best = None
        for entry in self.entries:
            for text, is_candidate in forms:
                cost = distance(text, entry["plate"])
                if cost > limit:
                    continue
                hit = {
                    "entry": entry,
                    "read": text,
                    "distance": round(cost, 3),
                    "exact": cost == 0.0 and not is_candidate,
                    "via": "candidate" if is_candidate else "read",
                }
                rank = (cost, 1 if is_candidate else 0)
                if best is None or rank < best[0]:
                    best = (rank, hit)
        return None if best is None else best[1]

    def describe(self):
        """What the UI shows about the list itself. Never the alerts."""
        self.refresh()
        try:
            shown = str(self.path.relative_to(config.ROOT))
        except ValueError:
            shown = str(self.path)
        return {
            "path": shown.replace("\\", "/"),
            "exists": self._mtime is not None,
            "count": len(self.entries),
            "plates": [dict(entry) for entry in self.entries],
            "skipped": list(self.skipped),
            "error": self.error,
            "loaded_ts": self.loaded_ts,
        }


def _short(item):
    text = str(item)
    return text if len(text) <= 60 else text[:57] + "..."


# ---------------------------------------------------------------- the checks


def _source_map(conn):
    rows = conn.execute("SELECT source_id, name, lat, lon FROM sources").fetchall()
    return {row["source_id"]: dict(row) for row in rows}


def _clock(text):
    """A stored timestamp as something to read in a sentence."""
    moment = from_iso(text)
    return "an unknown time" if moment is None else moment.strftime("%H:%M:%S")


def blacklist_hit(conn, sighting, blacklist, sources=None):
    """The alert a blacklisted plate raises, or None."""
    if not (sighting.get("plate_text") or sighting.get("plate_raw")):
        return None
    hit = blacklist.match(sighting)
    if hit is None:
        return None

    sources = _source_map(conn) if sources is None else sources
    where = sources.get(sighting["source_id"], {}).get("name") or sighting["source_id"]
    when = _clock(sighting.get("first_seen_ts"))
    entry = hit["entry"]
    reason = f" -- {entry['reason']}" if entry["reason"] else ""

    if hit["exact"]:
        severity = entry["severity"]
        detail = (
            f"{entry['plate']} is on the blacklist{reason}. Seen at {where}, {when}."
        )
    else:
        # Never critical. The read is not the blacklisted string, and whoever
        # reads this has to be able to see how far off it was and disagree.
        severity = _cap(entry["severity"], "warning")
        through = " in its alternatives" if hit["via"] == "candidate" else ""
        # The cost and the limit both, because a fuzzy hit is an opinion and
        # the person reading it has to see how strong an opinion it is. A
        # character difference is 1.00 and a glyph OCR confuses is 0.35, so
        # "0.35 of 1.00" says "one confusable character" without pretending
        # the weighted cost is a count of characters.
        limit = float(_setting("alert_match_distance", MATCH_DISTANCE))
        detail = (
            f"{hit['read']} at {where}, {when}, matches blacklisted "
            f"{entry['plate']}{through}{reason}. Not an exact read: match cost "
            f"{hit['distance']:.2f} of the {limit:.2f} allowed."
        )

    return {
        "kind": "blacklist",
        "severity": severity,
        "plate_text": entry["plate"],
        "sighting_ids": [sighting["sighting_id"]],
        "detail": detail,
        "created_ts": utc_now(),
    }


def _neighbours(conn, sighting, window_sec):
    """Sightings at OTHER placed sources close enough in time to be a transition.

    A length window and a time window in SQL keep this off the whole table; the
    confusion-weighted gate above decides. Only placed sources are fetched,
    because a transition with no distance has no speed and is not an alert.
    """
    moment = from_iso(sighting.get("first_seen_ts"))
    if moment is None:
        return []
    plate = normalize(sighting.get("plate_text") or sighting.get("plate_raw") or "")
    if not plate:
        return []

    low = to_iso(moment - timedelta(seconds=window_sec))
    high = to_iso(moment + timedelta(seconds=window_sec))
    return conn.execute(
        "SELECT s.*, src.name AS source_name, src.lat AS lat, src.lon AS lon "
        "FROM sightings s JOIN sources src ON src.source_id = s.source_id "
        "WHERE s.plate_text IS NOT NULL "
        "  AND s.source_id != ? "
        "  AND s.sighting_id != ? "
        "  AND src.lat IS NOT NULL AND src.lon IS NOT NULL "
        "  AND LENGTH(s.plate_text) BETWEEN ? AND ? "
        "  AND s.last_seen_ts >= ? AND s.first_seen_ts <= ?",
        (
            sighting["source_id"],
            sighting["sighting_id"],
            max(1, len(plate) - 3),
            len(plate) + 3,
            low,
            high,
        ),
    ).fetchall()


def transition_alerts(conn, sighting, sources=None):
    """Every impossible transition this sighting completes.

    A list rather than one answer: a vehicle arriving at a third camera can be
    impossible against both of the two before it, and reporting only the worst
    would hide one of them.
    """
    sources = _source_map(conn) if sources is None else sources
    here = sources.get(sighting["source_id"], {})
    if here.get("lat") is None or here.get("lon") is None:
        # An unplaced camera has no distance to anywhere. Not an error -- the
        # user has not put it on the map yet -- and not an alert either.
        return []

    limit = float(_setting("alert_match_distance", MATCH_DISTANCE))
    max_speed = float(_setting("alert_max_speed_kmh", MAX_SPEED_KMH))
    min_km = float(_setting("alert_min_distance_km", MIN_DISTANCE_KM))
    window = float(_setting("alert_transition_window_sec", TRANSITION_WINDOW_SEC))

    mine = plate_forms(sighting)
    if not mine:
        return []

    raised = []
    for row in _neighbours(conn, sighting, window):
        other = dict(row)
        cost, read_a, read_b = _closest(mine, plate_forms(other))
        if cost is None or cost > limit:
            continue

        km = haversine_km(here["lat"], here["lon"], other["lat"], other["lon"])
        if km is None or km < min_km:
            continue

        # Order the pair by the clock, not by which one was written first. A
        # file worker emits a track when it ends, so rows arrive out of order.
        first, second = other, sighting
        if (sighting.get("first_seen_ts") or "") < (other.get("first_seen_ts") or ""):
            first, second = sighting, other
        first_place = (
            sources.get(first["source_id"], {}).get("name") or first["source_id"]
        )
        second_place = (
            sources.get(second["source_id"], {}).get("name") or second["source_id"]
        )

        # Leaving one camera to arriving at the next, the same arithmetic the
        # Trace table shows. first_seen at both ends would charge the vehicle
        # for the time it spent inside the first camera's view.
        start = from_iso(first.get("last_seen_ts")) or from_iso(
            first.get("first_seen_ts")
        )
        end = from_iso(second.get("first_seen_ts"))
        if start is None or end is None:
            continue
        gap = (end - start).total_seconds()

        plate = sighting.get("plate_text") or read_a
        pair = sorted([sighting["sighting_id"], other["sighting_id"]])

        if gap <= 0:
            # Two separated cameras holding one vehicle at one instant. No speed
            # explains that, so there is nothing to compare against a limit --
            # this is the one transition that is impossible on its face.
            raised.append({
                "kind": "impossible_transition",
                "severity": "critical",
                "plate_text": plate,
                "sighting_ids": pair,
                "detail": (
                    f"{plate} was at {first_place} and {second_place} at the "
                    f"same moment, and they are {km:.2f} km apart."
                ),
                "created_ts": utc_now(),
            })
            continue

        speed = km / (gap / 3600.0)
        if speed <= max_speed:
            continue
        how = (
            ""
            if cost == 0.0
            else f" (read {read_a} and {read_b}, match cost {cost:.2f})"
        )
        raised.append({
            "kind": "impossible_transition",
            "severity": "warning",
            "plate_text": plate,
            "sighting_ids": pair,
            "detail": (
                f"{plate} was at {first_place} and then {second_place}, "
                f"{km:.2f} km away, {_gap_words(gap)} later{how} -- "
                f"{speed:.0f} km/h."
            ),
            "created_ts": utc_now(),
        })
    return raised


def _closest(forms_a, forms_b):
    """Cheapest confusion-weighted distance between two sets of stored reads."""
    best = (None, None, None)
    for text_a, _ in forms_a:
        for text_b, _ in forms_b:
            cost = distance(text_a, text_b)
            if best[0] is None or cost < best[0]:
                best = (cost, text_a, text_b)
    return best


def _gap_words(seconds):
    if seconds < 90:
        return f"{seconds:.1f} s"
    if seconds < 5400:
        return f"{seconds / 60:.1f} min"
    return f"{seconds / 3600:.1f} h"


# ------------------------------------------------------------------- writing


def _key(alert):
    """Canonical identity of an alert: its kind and the rows it is about."""
    return alert["kind"], json.dumps(sorted(alert["sighting_ids"]))


def record(conn, alert):
    """Insert one alert unless it is already there. Returns the stored row.

    Deduplication is not a nicety. A track the tracker hands back after its row
    was written is re-emitted, so the same sighting reaches this code more than
    once, and an alert list that repeats itself is one nobody reads.
    """
    kind, ids = _key(alert)
    existing = conn.execute(
        "SELECT alert_id FROM alerts WHERE kind = ? AND sighting_ids = ?",
        (kind, ids),
    ).fetchone()
    if existing is not None:
        return None
    cursor = conn.execute(
        "INSERT INTO alerts (kind, severity, plate_text, sighting_ids, detail, "
        "created_ts) VALUES (?, ?, ?, ?, ?, ?)",
        (
            kind,
            alert["severity"],
            alert["plate_text"],
            ids,
            alert["detail"],
            alert["created_ts"],
        ),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM alerts WHERE alert_id = ?", (cursor.lastrowid,)
    ).fetchone()
    return None if row is None else dict(row)


def evaluate(conn, sighting, blacklist, sources=None):
    """Every alert one committed sighting raises, written and returned.

    Called from the writer thread on the writer's connection, inside the commit
    path. It returns the stored rows so the caller can publish them; it does not
    publish anything itself.
    """
    if not sighting.get("plate_text") and not sighting.get("plate_raw"):
        # A sighting with no plate is valid and is not an alert. Neither check
        # has anything to work with.
        return []

    sources = _source_map(conn) if sources is None else sources
    found = []
    hit = blacklist_hit(conn, sighting, blacklist, sources)
    if hit is not None:
        found.append(hit)
    found.extend(transition_alerts(conn, sighting, sources))

    stored = []
    for alert in found:
        row = record(conn, alert)
        if row is not None:
            print(f"[alerts] {row['severity']}: {row['detail']}")
            stored.append(row)
    return stored


# ------------------------------------------------------------------- reading


def hydrate(conn, rows):
    """Attach the evidence an alert is about.

    An impossible transition is only readable as paired evidence -- both crops,
    both timestamps, the distance and the speed -- and none of that is in the
    alerts table, by contract. It is derived here from the sightings the alert
    names, so the table stays the frozen seven columns and the screen still gets
    what it has to show.
    """
    rows = [dict(row) for row in rows]
    wanted = set()
    for row in rows:
        row["sighting_ids"] = _ids(row.get("sighting_ids"))
        wanted.update(row["sighting_ids"])

    evidence = {}
    if wanted:
        ids = sorted(wanted)
        placeholders = ",".join("?" * len(ids))
        for found in conn.execute(
            f"SELECT s.*, src.name AS source_name, src.lat AS lat, src.lon AS lon "
            f"FROM sightings s LEFT JOIN sources src ON src.source_id = s.source_id "
            f"WHERE s.sighting_id IN ({placeholders})",
            tuple(ids),
        ).fetchall():
            evidence[found["sighting_id"]] = dict(found)

    for row in rows:
        stops = [evidence[i] for i in row["sighting_ids"] if i in evidence]
        stops.sort(key=lambda stop: (stop.get("first_seen_ts") or "", stop["sighting_id"]))
        row["sightings"] = stops
        # A sighting deleted with its source is named rather than dropped. An
        # alert that quietly refers to nothing is worse than one that says so.
        row["missing_sightings"] = [i for i in row["sighting_ids"] if i not in evidence]
        row["transition"] = (
            _leg(stops) if row["kind"] == "impossible_transition" else None
        )
    return rows


def _ids(packed):
    if isinstance(packed, list):
        return packed
    try:
        value = json.loads(packed or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _leg(stops):
    """The arithmetic behind an impossible transition, for the screen to show."""
    if len(stops) < 2:
        return None
    first, second = stops[0], stops[-1]
    km = haversine_km(
        first.get("lat"), first.get("lon"), second.get("lat"), second.get("lon")
    )
    start = from_iso(first.get("last_seen_ts")) or from_iso(first.get("first_seen_ts"))
    end = from_iso(second.get("first_seen_ts"))
    gap = None if start is None or end is None else (end - start).total_seconds()
    speed = None
    if km is not None and gap is not None and gap > 0:
        speed = round(km / (gap / 3600.0), 1)
    return {
        "from_sighting_id": first["sighting_id"],
        "to_sighting_id": second["sighting_id"],
        "from_source": first.get("source_name") or first.get("source_id"),
        "to_source": second.get("source_name") or second.get("source_id"),
        "distance_km": None if km is None else round(km, 4),
        "gap_seconds": None if gap is None else round(gap, 3),
        "speed_kmh": speed,
        "limit_kmh": float(_setting("alert_max_speed_kmh", MAX_SPEED_KMH)),
    }
