"""One plate, every camera that saw it, in the order it passed them.

A trajectory is the sightings of one vehicle sorted by time, with the gap
between consecutive stops worked out: how long, how far, and therefore how fast
the vehicle had to be moving. That last number is what the Trace table shows and
what P5's impossible-transition alert is built on -- 400 km/h between two
cameras means one of the two plate reads belongs to a different vehicle.

The sightings are gathered by fuzzy match, not by equality, because that is the
whole difficulty. The vehicle that camera one recorded as MH15HY2237 is the one
camera two recorded as MH15HY22, and a trajectory assembled by string equality
would draw two unrelated one-stop paths instead of one journey.

Coordinates are nullable by contract: a source the user has not placed on the
map yet has no lat/lon. That is not an error and not a reason to drop the stop
-- the vehicle was still seen there at that time. Distance and speed come back
as None for those legs and the UI says the source needs placing, rather than
drawing a line to the middle of the ocean.
"""

from math import asin, cos, radians, sin, sqrt

from app.db import from_iso
from app.matching import match_sightings

EARTH_RADIUS_KM = 6371.0


def haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in km, or None if either point is unplaced."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    phi1, phi2 = radians(lat1), radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = radians(lon2 - lon1)
    a = sin(d_phi / 2) ** 2 + cos(phi1) * cos(phi2) * sin(d_lambda / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))


def _sources(conn):
    rows = conn.execute(
        "SELECT source_id, name, lat, lon, heading_deg FROM sources"
    ).fetchall()
    return {row["source_id"]: dict(row) for row in rows}


def _seconds(earlier, later):
    """Elapsed seconds between two stored timestamps, or None."""
    start, end = from_iso(earlier), from_iso(later)
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def trajectory(conn, plate, min_score=0.72, limit=500):
    """Time-ordered path of one vehicle.

    Returns a dict rather than a bare list because the empty answer has to say
    something. A plate with no sightings and a plate whose sightings are all at
    unplaced sources are different situations, and the screen has to tell them
    apart to say what to do next.

        plate       the query, as asked
        stops       time-ordered sightings, each with its leg to the previous
        matched     how many sightings matched
        sources     how many distinct sources saw it
        placed      how many stops have coordinates
        span_seconds  first to last sighting
        distance_km   total over the legs that have coordinates

    Each stop carries source, coordinates, evidence crops, its match score and
    how it matched, plus the leg from the stop before it: gap_seconds,
    distance_km, speed_kmh. The first stop's leg fields are None, and so are the
    legs either side of an unplaced source.
    """
    matches = match_sightings(conn, plate, min_score)
    matches.sort(key=lambda row: (row["first_seen_ts"] or "", row["sighting_id"]))
    matches = matches[:limit]

    sources = _sources(conn)
    stops = []
    previous = None
    total_km = 0.0

    for match in matches:
        source = sources.get(match["source_id"], {})
        stop = {
            "sighting_id": match["sighting_id"],
            "source_id": match["source_id"],
            "source_name": source.get("name") or match["source_id"],
            "lat": source.get("lat"),
            "lon": source.get("lon"),
            "heading_deg": source.get("heading_deg"),
            "plate_text": match["plate_text"],
            "plate_raw": match["plate_raw"],
            "plate_conf": match["plate_conf"],
            "matched_text": match["matched_text"],
            "matched_via": match["matched_via"],
            "score": match["score"],
            "vehicle_type": match["vehicle_type"],
            "first_seen_ts": match["first_seen_ts"],
            "last_seen_ts": match["last_seen_ts"],
            "crop_path": match["crop_path"],
            "plate_crop_path": match["plate_crop_path"],
            "frames_voted": match["frames_voted"],
            "gap_seconds": None,
            "distance_km": None,
            "speed_kmh": None,
        }

        if previous is not None:
            # Leaving one camera to arriving at the next: last_seen there to
            # first_seen here. Using first_seen at both ends would charge the
            # vehicle for the time it spent inside the first camera's view and
            # quietly overstate every speed.
            gap = _seconds(previous["last_seen_ts"], stop["first_seen_ts"])
            # A negative gap is real and is left negative. It means the two
            # sightings overlap in time -- one camera split the vehicle into two
            # tracks, or two cameras with overlapping views both held it at once
            # -- and that is exactly what the number should say. Clamping it to
            # zero would hide a track split behind a plausible-looking journey.
            stop["gap_seconds"] = None if gap is None else round(gap, 3)
            km = haversine_km(
                previous["lat"], previous["lon"], stop["lat"], stop["lon"]
            )
            if km is not None:
                stop["distance_km"] = round(km, 4)
                total_km += km
                # A gap of zero is two cameras seeing one vehicle at the same
                # instant -- possible when their views overlap. Dividing by it
                # would report an infinite speed, which is a number nobody can
                # act on.
                if gap and gap > 0:
                    stop["speed_kmh"] = round(km / (gap / 3600.0), 2)

        stops.append(stop)
        previous = stop

    placed = sum(1 for stop in stops if stop["lat"] is not None)
    span = (
        _seconds(stops[0]["first_seen_ts"], stops[-1]["last_seen_ts"])
        if stops
        else None
    )

    return {
        "plate": plate,
        "matched": len(stops),
        "sources": len({stop["source_id"] for stop in stops}),
        "placed": placed,
        "span_seconds": None if span is None else round(span, 3),
        "distance_km": round(total_km, 4) if placed > 1 else None,
        "stops": stops,
    }
