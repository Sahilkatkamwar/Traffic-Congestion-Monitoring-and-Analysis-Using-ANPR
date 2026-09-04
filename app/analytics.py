"""What the sightings add up to, over one window of time.

Five panels, one row set. Every number the Insights screen shows comes from a
single fetch of the sightings inside the chosen window, computed in this module
-- not five queries that could each answer for a slightly different slice. A
shared time filter is only shared if the panels physically cannot disagree
about what it selected, and one row set is what makes that true rather than
intended.

Three things this module refuses to invent:

**A heatmap of cameras is not a heatmap of traffic.** The database records
where a camera is, never where a vehicle was between two of them. So the
density surface is built on source coordinates weighted by how many vehicles
each source saw, and the response says how many sightings could not be placed
at all. Smearing a vehicle along a guessed route would draw traffic onto roads
nobody observed.

**Origin-destination flows are assembled by fuzzy match, never by plate
equality.** The same vehicle leaves camera one as MH15HY227 and arrives at
camera two as MH15HY22, and an equality test draws no flow at all. The grouping
threshold is stricter than the Trace screen's retrieval floor on purpose: on
Trace a person reads the ranked list and decides, here nobody does -- a wrong
grouping becomes a flow line asserting a journey that never happened.

**Density is reported two ways, because a raw count ranks a 200-second clip
against a 20-second one.** Both the count and the rate per hour the source was
actually producing travel with every source, and the screen says which one it
is sorting by.

Read-only. Nothing in here writes.
"""

from collections import defaultdict
from datetime import datetime, timezone

from app.db import from_iso, to_iso
from app.matching import similarity, skeleton
from app.trajectory import haversine_km

# Bucket widths for the count-over-time chart, in seconds. The smallest one
# that keeps the bar count under MAX_BUCKETS wins, so a 40-second clip is read
# in seconds and a fortnight of footage in days -- without either being a
# setting anybody has to know about.
BUCKET_LADDER = (
    1, 2, 5, 10, 15, 30,
    60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200,
    86400, 172800, 604800,
)
MAX_BUCKETS = 90

# Two reads scoring this alike are treated as one vehicle for the flow lines.
#
# Measured on the application database (34 plate reads, 13 distinct strings, 78
# pairs): the one pair that is genuinely one vehicle -- MH15HY22 and MH15HY227,
# the same car read twice, which P4d already records -- scores 0.889, and the
# highest-scoring pair that is two different vehicles, MH07A3866 against
# MH07T3336, scores 0.667. Every threshold from 0.68 to 0.88 produces the
# identical grouping, so this sits in the middle of that band rather than on
# either edge of it.
#
# Deliberately above the 0.72 floor /api/search uses. That floor is for
# retrieval, where a person reads the candidates and picks one; this one decides
# silently and its answer is drawn on a map as a journey.
LINK_SCORE = 0.80

# Stage-one blocking for the grouping below: two reads more than this many
# characters apart in length are not scored against each other at all.
_LENGTH_WINDOW = 3


# ----------------------------------------------------------------- the window


def parse_ts(value):
    """A timestamp from a query string, in the format the rows are stored in.

    Returns (text, error). Anything ISO-8601 is accepted and normalised, so the
    comparison against stored timestamps is between two strings of the same
    shape -- which is the only thing that makes the lexicographic ordering the
    schema relies on safe.
    """
    if value is None or str(value).strip() == "":
        return None, None
    text = str(value).strip()
    try:
        return to_iso(from_iso(text)), None
    except (ValueError, TypeError):
        return None, (
            f"{text!r} is not a time this understands. "
            "Use something like 2026-09-01T15:19:00Z."
        )


def extent(conn):
    """The whole of the data, ignoring any filter.

    The window control needs this to say what there is to look at, and an empty
    database has to be able to say so rather than render a range of nothing.
    """
    row = conn.execute(
        "SELECT MIN(first_seen_ts), MAX(last_seen_ts), COUNT(*) FROM sightings"
    ).fetchone()
    return {"first": row[0], "last": row[1], "sightings": row[2]}


def pick_bucket(span_seconds):
    """Bucket width for a span, off the ladder above."""
    if not span_seconds or span_seconds <= 0:
        return BUCKET_LADDER[0]
    for width in BUCKET_LADDER:
        if span_seconds / width <= MAX_BUCKETS:
            return width
    return BUCKET_LADDER[-1]


def _epoch(text):
    at = from_iso(text)
    return None if at is None else at.timestamp()


def _at(epoch_seconds):
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


# ------------------------------------------------------------------ the rows


def _rows(conn, start, end):
    """Every sighting in the window, fetched once.

    Filtered on first_seen_ts: a sighting belongs to the moment the vehicle
    arrived, and a track still open when the window closed is not retrospectively
    moved into the next one.
    """
    sql = (
        "SELECT sighting_id, source_id, track_id, plate_text, plate_conf, "
        "       vehicle_type, first_seen_ts, last_seen_ts "
        "FROM sightings"
    )
    clauses, params = [], []
    if start is not None:
        clauses.append("first_seen_ts >= ?")
        params.append(start)
    if end is not None:
        clauses.append("first_seen_ts <= ?")
        params.append(end)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY first_seen_ts, sighting_id"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _sources(conn):
    return {
        row["source_id"]: dict(row)
        for row in conn.execute(
            "SELECT source_id, name, kind, lat, lon, status, fps FROM sources"
        ).fetchall()
    }


# ---------------------------------------------------------------- the panels


def counts_over_time(rows, start, end, bucket):
    """Vehicles per bucket, split by whether a plate was read.

    Buckets are aligned to the epoch rather than to the first sighting, so a
    boundary is a round time on a clock and two windows over the same footage
    line up instead of being offset by whenever each query happened to begin.

    Empty buckets are emitted. A gap in traffic is a fact about the footage, and
    a chart that closes it up draws a busier road than the one that was filmed.
    """
    stamps = [_epoch(row["first_seen_ts"]) for row in rows]
    stamps = [value for value in stamps if value is not None]
    if not stamps:
        return []

    first = _epoch(start) if start else None
    last = _epoch(end) if end else None
    first = min(stamps) if first is None else min(first, min(stamps))
    last = max(stamps) if last is None else max(last, max(stamps))

    origin = (first // bucket) * bucket
    count = int((last - origin) // bucket) + 1
    # A window far wider than the data inside it would otherwise emit thousands
    # of empty buckets. Beyond a sane multiple the range is taken from the rows.
    if count > MAX_BUCKETS * 4:
        origin = (min(stamps) // bucket) * bucket
        count = int((max(stamps) - origin) // bucket) + 1

    buckets = [
        {"start": to_iso(_at(origin + index * bucket)), "total": 0, "plated": 0}
        for index in range(max(count, 1))
    ]
    for row in rows:
        at = _epoch(row["first_seen_ts"])
        if at is None:
            continue
        index = int((at - origin) // bucket)
        if 0 <= index < len(buckets):
            buckets[index]["total"] += 1
            if row["plate_text"]:
                buckets[index]["plated"] += 1
    for entry in buckets:
        entry["unread"] = entry["total"] - entry["plated"]
    return buckets


def type_distribution(rows):
    """How many of each vehicle type, biggest first.

    A null type is `unknown` and is its own class rather than dropped: the
    detector found a vehicle and could not say what kind, which is a different
    statement from there being no vehicle.
    """
    counts = defaultdict(int)
    for row in rows:
        counts[(row["vehicle_type"] or "unknown").lower()] += 1
    total = sum(counts.values())
    return [
        {
            "vehicle_type": name,
            "count": count,
            "share": round(count / total, 4) if total else 0.0,
        }
        for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]


def source_density(rows, sources):
    """Every source, ranked by how much it saw in this window.

    Two numbers, because they rank differently and both are honest. `count` is
    what the window contains. `per_hour` divides it by the time that source was
    actually producing inside the window, which is the only way a 20-second clip
    and a 200-second one can be compared at all -- and it is None, never zero,
    for a source whose sightings share one instant, such as a still image.

    A source with nothing in the window stays in the list at zero. A camera that
    saw no traffic is a result.
    """
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["source_id"]].append(row)
    total = len(rows)

    ranked = []
    for source_id, source in sources.items():
        mine = grouped.get(source_id, [])
        first = min((r["first_seen_ts"] for r in mine if r["first_seen_ts"]), default=None)
        last = max((r["last_seen_ts"] for r in mine if r["last_seen_ts"]), default=None)
        span = None
        if first and last:
            span = round((_epoch(last) or 0) - (_epoch(first) or 0), 3)
        ranked.append({
            "source_id": source_id,
            "name": source.get("name") or source_id,
            "kind": source.get("kind"),
            "status": source.get("status"),
            "lat": source.get("lat"),
            "lon": source.get("lon"),
            "count": len(mine),
            "plated": sum(1 for r in mine if r["plate_text"]),
            "first_seen_ts": first,
            "last_seen_ts": last,
            "span_seconds": span,
            "per_hour": (
                round(len(mine) / (span / 3600.0), 1) if span and span > 0 else None
            ),
            "share": round(len(mine) / total, 4) if total else 0.0,
        })

    # Sightings whose source row has been deleted. They are still sightings and
    # the totals still include them, so they get a row saying so rather than
    # quietly falling out of the ranking and making the columns disagree.
    for source_id, mine in grouped.items():
        if source_id in sources:
            continue
        ranked.append({
            "source_id": source_id,
            "name": f"{source_id} (source removed)",
            "kind": None, "status": None, "lat": None, "lon": None,
            "count": len(mine),
            "plated": sum(1 for r in mine if r["plate_text"]),
            "first_seen_ts": None, "last_seen_ts": None,
            "span_seconds": None, "per_hour": None,
            "share": round(len(mine) / total, 4) if total else 0.0,
        })

    ranked.sort(key=lambda entry: (-entry["count"], entry["name"]))
    return ranked


def heat(ranked):
    """The density surface, and what it cannot show.

    One point per placed source, weighted by what that source saw. There is no
    third coordinate to invent: a sighting knows which camera saw it and the
    camera knows where it is, and that is the whole of the position information
    this database holds.
    """
    points = [
        {
            "source_id": entry["source_id"],
            "name": entry["name"],
            "lat": entry["lat"],
            "lon": entry["lon"],
            "count": entry["count"],
        }
        for entry in ranked
        if entry["lat"] is not None and entry["lon"] is not None and entry["count"] > 0
    ]
    return {
        "points": points,
        "max": max((point["count"] for point in points), default=0),
        # Said plainly, because a density map missing half the traffic must not
        # look like a density map of all of it.
        "unplaced_sightings": sum(
            entry["count"] for entry in ranked if entry["lat"] is None
        ),
        "unplaced_sources": sum(
            1 for entry in ranked if entry["lat"] is None and entry["count"] > 0
        ),
    }


# ------------------------------------------------------------------ od flows


def _group_vehicles(rows, min_score):
    """One group per vehicle, gathered by confusion-weighted similarity.

    Union-find over the plated rows. Stage one blocks on a shared
    confusion-group bigram and a length window, exactly as `matching._rows`
    does, so two reads of one plate survive the prefilter together even when
    they share no exact substring -- and the quadratic comparison only ever runs
    inside a block.
    """
    plated = [row for row in rows if row["plate_text"]]
    parent = list(range(len(plated)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    blocks = defaultdict(list)
    for index, row in enumerate(plated):
        text = skeleton(row["plate_text"])
        grams = {text[i:i + 2] for i in range(len(text) - 1)} or {text}
        for gram in grams:
            blocks[gram].append(index)

    compared = set()
    for members in blocks.values():
        for position, a in enumerate(members):
            for b in members[position + 1:]:
                pair = (a, b)
                if pair in compared:
                    continue
                compared.add(pair)
                if find(a) == find(b):
                    continue
                if abs(len(plated[a]["plate_text"]) - len(plated[b]["plate_text"])) > _LENGTH_WINDOW:
                    continue
                if similarity(plated[a]["plate_text"], plated[b]["plate_text"]) >= min_score:
                    union(a, b)

    groups = defaultdict(list)
    for index, row in enumerate(plated):
        groups[find(index)].append(row)
    # Each group in time order, so consecutive members are consecutive stops.
    return [
        sorted(members, key=lambda r: (r["first_seen_ts"] or "", r["sighting_id"]))
        for members in groups.values()
    ]


def _median(values):
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def od_flows(rows, sources, min_score=LINK_SCORE):
    """Where vehicles went next, weighted by how many did.

    A flow is one vehicle's consecutive pair of stops at two *different*
    sources. Directed: A to B and B to A are two flows, because they are two
    different facts about a road.

    A flow with an unplaced source at either end has no line to draw and is
    counted rather than dropped -- the journey happened, the map simply cannot
    show it until the camera is placed.
    """
    groups = _group_vehicles(rows, min_score)

    links = defaultdict(lambda: {"count": 0, "seconds": [], "vehicles": set()})
    journeys = 0
    for members in groups:
        moved = False
        for previous, current in zip(members, members[1:]):
            if previous["source_id"] == current["source_id"]:
                continue
            moved = True
            entry = links[(previous["source_id"], current["source_id"])]
            entry["count"] += 1
            entry["vehicles"].add(members[0]["sighting_id"])
            if previous["last_seen_ts"] and current["first_seen_ts"]:
                entry["seconds"].append(
                    (_epoch(current["first_seen_ts"]) or 0)
                    - (_epoch(previous["last_seen_ts"]) or 0)
                )
        if moved:
            journeys += 1

    out = []
    undrawable = 0
    for (origin, destination), entry in links.items():
        a = sources.get(origin, {})
        b = sources.get(destination, {})
        km = haversine_km(a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon"))
        seconds = _median(entry["seconds"])
        speed = None
        if km is not None and seconds and seconds > 0:
            speed = round(km / (seconds / 3600.0), 2)
        drawable = km is not None
        if not drawable:
            undrawable += 1
        out.append({
            "from_source": origin,
            "from_name": a.get("name") or origin,
            "from_lat": a.get("lat"),
            "from_lon": a.get("lon"),
            "to_source": destination,
            "to_name": b.get("name") or destination,
            "to_lat": b.get("lat"),
            "to_lon": b.get("lon"),
            "count": entry["count"],
            "vehicles": len(entry["vehicles"]),
            "median_seconds": None if seconds is None else round(seconds, 3),
            "distance_km": None if km is None else round(km, 4),
            "median_speed_kmh": speed,
            "drawable": drawable,
        })

    out.sort(key=lambda flow: (-flow["count"], flow["from_name"], flow["to_name"]))
    return {
        "links": out,
        "max": max((flow["count"] for flow in out), default=0),
        # Every vehicle the window holds a plate read for, however many times it
        # was seen. `journeys` is the subset that was seen at more than one
        # source -- the rest have no flow to contribute.
        "vehicles": len(groups),
        "journeys": journeys,
        "undrawable": undrawable,
        "min_score": min_score,
    }


# ------------------------------------------------------------------ assembly


def insights(conn, start=None, end=None, bucket=None, min_score=LINK_SCORE):
    """Every panel, over one window, from one row set."""
    rows = _rows(conn, start, end)
    sources = _sources(conn)

    stamps = [value for value in (_epoch(r["first_seen_ts"]) for r in rows) if value]
    covered = 0.0
    if stamps:
        opened = _epoch(start) if start else min(stamps)
        closed = _epoch(end) if end else max(stamps)
        covered = max(0.0, (closed or 0) - (opened or 0))
    width = bucket or pick_bucket(covered)

    ranked = source_density(rows, sources)
    plated = sum(1 for row in rows if row["plate_text"])

    return {
        "window": {
            "from": start,
            "to": end,
            "bucket_seconds": width,
            "covered_seconds": round(covered, 3),
        },
        "extent": extent(conn),
        "totals": {
            "sightings": len(rows),
            "plated": plated,
            "unread": len(rows) - plated,
            "sources_seen": sum(1 for entry in ranked if entry["count"] > 0),
            "sources_total": len(sources),
            "sources_placed": sum(1 for s in sources.values() if s.get("lat") is not None),
        },
        "buckets": counts_over_time(rows, start, end, width),
        "types": type_distribution(rows),
        "sources": ranked,
        "heat": heat(ranked),
        "flows": od_flows(rows, sources, min_score),
    }
