"""P10. The base map, and the administrative boundaries drawn over it.

Two upstream services, and they are used differently on purpose.

**MapTiler** needs a key, and `MAPTILER_API_KEY` follows the same rule
`TEXTBEE_API_KEY` does: environment only, never in `config/settings.yaml`
because that file is in git, and **never returned by any API response**. The
last clause is what decides the design. A tile URL template with the key in it
IS the key -- putting one in `/api/map/config` hands the credential to every
browser that opens the screen and to anything watching the network. So the
browser is given a template that points back here, and this module fetches the
tile with the key attached server-side. The key does not leave the process.

**Overpass** is keyless, and is proxied for a different reason: the cache. The
public instance is a shared free service with a rate limit, and PHASE2.md's
trap list names hitting it mid-demo. A cache in one browser tab helps that tab;
a cache here helps every tab, survives a reload, and is the only place a
minimum interval between upstream calls can actually be enforced.

**With no key set the browser talks to OpenStreetMap directly.** There is no
credential to hide, and proxying somebody else's free tiles through this server
would hide the real client from them, which their tile usage policy exists to
prevent. Degrade, don't break, the same way the P0-P5 webcam check does.

Nothing here is written to the database. The tile cache is files under
`paths.tile_cache` and the boundary cache is a dict in this process; both are
disposable and neither is evidence.
"""

import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from app import config

# Same pattern as notify.ENV_API_KEY: named once, reported by name only.
ENV_KEY = "MAPTILER_API_KEY"

# Cloudflare and friends refuse the stdlib's default `Python-urllib/3.11` --
# measured on TextBee in P5 and there is no reason to find that out the hard
# way twice. Same string, one client.
USER_AGENT = "anpr-city/1.0"

MAPTILER_HOST = "https://api.maptiler.com"
# Dark, because the base surface is a deep slate and a bright basemap fights
# every panel floating over it -- the same reason the keyless Esri layer this
# replaces was a dark canvas. Overridable in settings: a style is not a
# credential.
DEFAULT_STYLE = "streets-v2-dark"

OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
OSM_ATTRIBUTION = (
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    "contributors"
)
MAPTILER_ATTRIBUTION = (
    '&copy; <a href="https://www.maptiler.com/copyright/">MapTiler</a> '
    '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> '
    "contributors"
)

DEFAULT_OVERPASS = "https://overpass-api.de/api/interpreter"

# India's administrative ladder, as the OSM data actually carries it --
# MEASURED, not read off the wiki, because the two disagree and the first live
# run of this module returned zero outlines as a result.
#
# scratch/p10_levels_probe.py asked Overpass `is_in` at seven points in six
# states (log in scratch/p10_levels_probe.log). Every one of them answers:
#
#     2   India
#     4   Maharashtra / Karnataka / Rajasthan / Kerala / Delhi
#     5   Nashik District, Pune District, Nagpur, Jaipur, Ernakulam
#     6   Nashik Subdistrict, Jaipur Tehsil, Nagpur Urban Taluka
#     8   Nagpur City, Jaipur Municipal Corporation
#
# So PHASE2.md's "state -> district -> taluka/tehsil -> village/nagar" is
# **4, 5, 6, 8** in this data. The OSM wiki says districts are 6 and tehsils
# are 7; in India they are not, and asking for 7 got a taluka query that
# matched almost nothing. Level 7 does exist in a few places (Bengaluru is the
# one the probe found) and level 10 is a ward, so both are understood when they
# come back and neither is asked for.
LEVEL_NAMES = {
    "2": "Country",
    "3": "Region",
    "4": "State",
    "5": "District",
    "6": "Taluka",
    "7": "Block or circle",
    "8": "Town or village",
    "9": "Ward",
    "10": "Ward",
}

# Which levels are drawn at which zoom, and how much map may be asked about at
# once. Both numbers are MEASURED against the live Overpass, because
# `out geom` returns full-resolution geometry and the cost is not obvious:
#
#     snapped area   levels          from Overpass   time
#     264 deg2 (z7)  4 states            27.07 MB    11.3s
#      72 deg2 (z8)  4 states            12.82 MB     5.8s
#      16 deg2 (z9)  5,6 dist+taluka     14.05 MB     7.2s
#       7 deg2 (z10) 5,6 dist+taluka      7.36 MB     4.6s
#       1.5 deg2(z11) 5,6 dist+taluka     3.88 MB     3.3s
#       0.5 deg2(z12) 5,6 dist+taluka     2.46 MB     3.0s
#       0.25 deg2(z13) 6,8 taluka+town    0.83 MB     3.0s
#
# So **state outlines are not drawn at all**: one costs 12-27 MB off a shared
# free service, per viewport, and no amount of thinning helps because the
# download is the cost. PHASE2.md's ladder is still all there -- the state and
# the country are named by the click, which is `is_in` asking for tags only and
# comes back in about a second -- but what gets DRAWN stops at the district.
#
# That is a deliberate departure from "state -> district -> taluka -> village"
# read as four things to draw, and the number above is the reason.
ZOOM_LEVELS = (
    (13, ("5", "6")),     # districts and the talukas inside them
    (99, ("6", "8")),     # talukas and the towns and villages inside them
)

# The most map that may be asked about at once, in degrees squared. 2.0 admits
# a z11 view (1.5) and refuses a z10 one (7.0), which is where the table above
# crosses from ~4 MB into ~7 MB. Below that zoom the screen says to zoom in
# rather than quietly asking for something this slow.
MAX_BBOX_DEG2 = 2.0

# How far a requested viewport is grown out to a fixed grid before it is asked
# for. Panning inside one cell is then a cache hit rather than a new query,
# which is PHASE2.md's "do not refetch on every pan tick" -- enforced here
# rather than trusted to a debounce in one browser.
#
# One step rather than one per zoom band: only z11 and finer are served at all,
# and they all sit inside a single band, so a table of steps would be four
# numbers of which three could never be reached.
GRID_DEG = 0.25


def _map_settings():
    return config.load_settings().get("map") or {}


def _setting(key, fallback):
    value = _map_settings().get(key)
    return fallback if value is None else value


def api_key():
    """The MapTiler key, or None. Never leaves this module."""
    return os.environ.get(ENV_KEY, "").strip() or None


def style():
    return str(_setting("style", DEFAULT_STYLE)).strip() or DEFAULT_STYLE


def tile_cache_dir():
    configured = config.load_settings()["paths"].get("tile_cache")
    return configured if configured is not None else config.ROOT / "data" / "tilecache"


def describe():
    """What the browser is told about the base map. No credential in it.

    `key_env` is the variable's NAME, which is the actionable half and is for
    whoever runs the server; `configured` says whether it is set. The value
    itself appears in no field of this dict, and the suite asserts that by
    setting the key to a known string and looking for that string in the whole
    serialised answer.
    """
    if api_key():
        return {
            "provider": "maptiler",
            # Points back here. The key is attached on the way out, in-process.
            "tile_url": "/api/map/tiles/{z}/{x}/{y}.png",
            "attribution": MAPTILER_ATTRIBUTION,
            "max_zoom": 19,
            "style": style(),
            "configured": True,
            "key_env": ENV_KEY,
            "reason": "Street-level MapTiler tiles.",
            "setup_detail": None,
        }
    return {
        "provider": "osm",
        "tile_url": OSM_TILES,
        "attribution": OSM_ATTRIBUTION,
        "max_zoom": 19,
        "style": None,
        "configured": False,
        "key_env": ENV_KEY,
        "reason": "Standard OpenStreetMap tiles.",
        "setup_detail": (
            f"{ENV_KEY} not set in the environment. Set it and restart the app "
            f"for street-level MapTiler tiles."
        ),
    }


# --------------------------------------------------------------- the network

class UpstreamError(Exception):
    """An upstream service refused, or could not be reached.

    Carries the sentence the screen renders, so nothing above this has to
    invent one out of a status code.
    """


def _open(url, data=None, timeout=30.0):
    """One HTTP call, stdlib only.

    `requests` is installed in env\\ but is not in requirements.txt, so it is
    not used here -- the same reason app/notify.py talks to TextBee through
    urllib. No dependency was added and none could be.
    """
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return (
            response.status,
            response.read(),
            response.headers.get("Content-Type", ""),
        )


# Everything upstream goes through this name so a verification run can replace
# it with a stub. The suites must never reach the network -- the same rule that
# keeps scratch/p5_textbee_test.py out of every one of them.
fetch = _open


# ---------------------------------------------------------------- tile proxy

_WHOLE_NUMBER = re.compile(r"^\d+$")


def _tile_path(z, x, y):
    return tile_cache_dir() / style() / str(z) / str(x) / f"{y}.png"


def _cache_tile(path, payload):
    """Write a fetched tile, and keep the cache from growing without end.

    Eviction is oldest-first by modification time, in one sweep when the count
    goes over the cap, because a tile is regenerable and losing one costs a
    refetch and nothing else. `tile_cache: false` turns the whole thing off.
    """
    cap = int(_setting("tile_cache_max", 4000))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside it and rename, so a half-written tile is never served:
        # two browser tabs asking for the same tile at once is ordinary.
        temporary = path.with_suffix(".part")
        temporary.write_bytes(payload)
        temporary.replace(path)
    except OSError as exc:
        print(f"[map] tile cache write failed ({exc}); serving without a cache")
        return

    try:
        files = list(tile_cache_dir().rglob("*.png"))
        if len(files) <= cap:
            return
        files.sort(key=lambda p: p.stat().st_mtime)
        for stale in files[: max(1, len(files) // 10)]:
            stale.unlink(missing_ok=True)
    except OSError:
        # A cache that cannot be swept is still a cache that works.
        pass


def tile(z, x, y):
    """One base-map tile, with the key attached here rather than in a browser.

    Returns (payload, content_type, cached). Raises UpstreamError carrying a
    sentence a person can act on.
    """
    key = api_key()
    if not key:
        raise UpstreamError(
            "No map key is set up on this server, so tiles come straight from "
            "OpenStreetMap instead and nothing is proxied through here."
        )
    for name, value in (("z", z), ("x", x), ("y", y)):
        if not _WHOLE_NUMBER.match(str(value)):
            raise UpstreamError(f"Tile {name} must be a whole number, not {value!r}.")
    z, x, y = int(z), int(x), int(y)
    if not 0 <= z <= 22:
        raise UpstreamError(f"Zoom {z} is outside the 0-22 a tile can exist at.")
    span = 1 << z
    if not (0 <= x < span and 0 <= y < span):
        raise UpstreamError(f"There is no tile {x},{y} at zoom {z}.")

    path = _tile_path(z, x, y)
    use_cache = bool(_setting("tile_cache", True))
    if use_cache:
        try:
            if path.is_file():
                return path.read_bytes(), "image/png", True
        except OSError:
            pass

    url = (
        f"{MAPTILER_HOST}/maps/{urllib.parse.quote(style())}/{z}/{x}/{y}.png"
        f"?key={urllib.parse.quote(key)}"
    )
    try:
        status, payload, content_type = fetch(
            url, timeout=float(_setting("timeout_seconds", 20))
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise UpstreamError(
                f"MapTiler refused the request. The key in {ENV_KEY} is wrong, "
                f"expired, or out of quota."
            ) from exc
        raise UpstreamError(
            f"MapTiler answered {exc.code} for tile {z}/{x}/{y}."
        ) from exc
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        raise UpstreamError(
            f"Could not reach MapTiler ({exc}). Check this machine is online."
        ) from exc

    if status != 200:
        raise UpstreamError(f"MapTiler answered {status} for tile {z}/{x}/{y}.")
    if use_cache:
        _cache_tile(path, payload)
    return payload, (content_type or "image/png").split(";")[0].strip(), False


# ----------------------------------------------------------------- boundaries

def levels_for_zoom(zoom):
    """Which admin levels are worth asking for at this zoom."""
    try:
        zoom = float(zoom)
    except (TypeError, ValueError):
        zoom = 11.0
    for ceiling, levels in ZOOM_LEVELS:
        if zoom < ceiling:
            return levels
    return ZOOM_LEVELS[-1][1]


def snap_bbox(south, west, north, east, zoom):
    """Grow a viewport out to the fixed grid cell it sits in.

    This is the whole of "do not refetch on every pan tick". A browser that
    nudges the map fifty metres asks the same snapped question and gets the
    cached answer; one that crosses a cell edge asks a new question, once. It
    also means the answer covers more than the viewport, so a small pan has
    nothing new to draw that it did not already have.
    """
    step = GRID_DEG
    return (
        math.floor(south / step) * step,
        math.floor(west / step) * step,
        math.ceil(north / step) * step,
        math.ceil(east / step) * step,
    )


def _overpass_url():
    return str(_setting("overpass_url", DEFAULT_OVERPASS))


class _Cache:
    """Answers keyed by the question, with a clock and a floor on the rate.

    Both halves matter. The TTL keeps an administrative boundary -- which moves
    on the order of years -- from being asked for twice in one session. The
    minimum interval is what the public Overpass instance actually asks for,
    and it belongs where every tab in every browser passes through it rather
    than in one of them.
    """

    def __init__(self):
        self._entries = {}
        self._lock = threading.Lock()
        self._last_call = 0.0

    # time.monotonic, not time.time, for both the age and the rate floor. Two
    # reasons and they are separate: a wall clock can be stepped backwards by
    # an NTP correction, which would make a cached answer look like it arrived
    # in the future; and on Windows time.time() advances in ~16ms jumps, which
    # is coarse enough to measure a 400ms interval as 390ms.
    def get(self, key, ttl):
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return None, False
        fetched_at, payload = entry
        return payload, (time.monotonic() - fetched_at) <= ttl

    def put(self, key, payload):
        with self._lock:
            self._entries[key] = (time.monotonic(), payload)

    def wait_turn(self, interval):
        """Hold the caller until the upstream may be asked again.

        Looped rather than one `time.sleep(gap)`, because on Windows a sleep
        returns early: measured on this machine, `time.sleep(0.4)` comes back
        after 0.390s, which is the ~15.6ms timer tick rounding down. One sleep
        would make this floor "about a second" instead of a second, and a floor
        that is approximate is a floor somebody has to re-measure later. Two
        iterations at most.
        """
        if interval <= 0:
            return
        with self._lock:
            deadline = self._last_call + interval
            while True:
                gap = deadline - time.monotonic()
                if gap <= 0:
                    break
                time.sleep(min(gap, interval))
            self._last_call = time.monotonic()

    def clear(self):
        with self._lock:
            self._entries.clear()
            self._last_call = 0.0

    def __len__(self):
        with self._lock:
            return len(self._entries)


CACHE = _Cache()


def _ask_overpass(query, cache_key):
    """One Overpass question: cached, rate-limited, and stale-on-failure.

    Returns (parsed, cached, stale). A failure with a stale answer already in
    hand serves the stale answer and says so, because a boundary drawn from an
    hour ago is right and an empty map is not.
    """
    ttl = float(_setting("boundary_ttl_seconds", 21600))
    payload, fresh = CACHE.get(cache_key, ttl)
    if payload is not None and fresh:
        return payload, True, False

    def stale_or_raise(detail):
        if payload is not None:
            print(f"[map] {detail} -- serving the boundaries already in hand")
            return payload, True, True
        raise UpstreamError(detail)

    CACHE.wait_turn(float(_setting("boundary_min_interval_seconds", 1.0)))
    try:
        status, body, _ = fetch(
            _overpass_url(),
            data=urllib.parse.urlencode({"data": query}).encode("utf-8"),
            timeout=float(_setting("overpass_timeout_seconds", 40)),
        )
    except urllib.error.HTTPError as exc:
        return stale_or_raise(
            "The public boundary service is busy. Boundaries load again in a "
            "minute; the map itself is unaffected."
            if exc.code in (429, 504)
            else f"The boundary service answered {exc.code}."
        )
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        return stale_or_raise(
            f"Could not reach the boundary service ({exc}). Check this machine "
            f"is online; the map itself is unaffected."
        )

    if status != 200:
        return stale_or_raise(f"The boundary service answered {status}.")
    try:
        parsed = json.loads(body.decode("utf-8", "replace"))
    except ValueError as exc:
        return stale_or_raise(
            f"The boundary service sent something that is not an answer ({exc})."
        )

    CACHE.put(cache_key, parsed)
    return parsed, False, False


def _query_for(south, west, north, east, levels):
    box = f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"
    clauses = "\n".join(
        f'  relation["boundary"="administrative"]["admin_level"="{level}"]({box});'
        for level in levels
    )
    timeout = int(float(_setting("overpass_timeout_seconds", 40)))
    return f"[out:json][timeout:{timeout}];\n(\n{clauses}\n);\nout geom;"


def _stitch(ways):
    """Join member ways end to end into rings.

    Overpass hands a boundary relation back as a bag of ways in no particular
    order and no particular direction, so they are walked: take one, keep
    attaching whichever unused way shares an endpoint, flipping it if that is
    what it takes, until nothing else fits. A ring that closes is a polygon; a
    ring that does not is kept as a line rather than force-closed, which would
    draw a shortcut across a district that is simply cut by the viewport.
    """
    remaining = [list(way) for way in ways if len(way) >= 2]
    rings = []
    while remaining:
        chain = remaining.pop()
        changed = True
        while changed:
            changed = False
            for index, candidate in enumerate(remaining):
                if chain[-1] == candidate[0]:
                    chain.extend(candidate[1:])
                elif chain[-1] == candidate[-1]:
                    chain.extend(list(reversed(candidate))[1:])
                elif chain[0] == candidate[-1]:
                    chain[:0] = candidate[:-1]
                elif chain[0] == candidate[0]:
                    chain[:0] = list(reversed(candidate))[:-1]
                else:
                    continue
                remaining.pop(index)
                changed = True
                break
        rings.append({
            "closed": len(chain) > 3 and chain[0] == chain[-1],
            "points": chain,
        })
    return rings


def simplify(points, tolerance):
    """Douglas-Peucker, in degrees, iteratively rather than recursively.

    A district boundary out of Overpass is tens of thousands of points at a
    fidelity no screen can show. Dropping the ones that sit on a line between
    their neighbours costs nothing at the zoom this is drawn at, and it is what
    keeps a browser from stalling on a pan. Both endpoints always survive, so a
    ring that was closed stays closed.
    """
    if tolerance <= 0 or len(points) < 3:
        return list(points)

    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        ax, ay = points[start]
        bx, by = points[end]
        dx, dy = bx - ax, by - ay
        span = math.hypot(dx, dy)
        worst, worst_at = -1.0, None
        for index in range(start + 1, end):
            px, py = points[index]
            if span == 0:
                distance = math.hypot(px - ax, py - ay)
            else:
                distance = abs(dy * px - dx * py + bx * ay - by * ax) / span
            if distance > worst:
                worst, worst_at = distance, index
        if worst > tolerance and worst_at is not None:
            keep[worst_at] = True
            stack.append((start, worst_at))
            stack.append((worst_at, end))
    return [point for point, kept in zip(points, keep) if kept]


def tolerance_for_zoom(zoom):
    """How much detail to drop, in degrees, for the zoom being drawn.

    Roughly one screen pixel: the world is 256 * 2^z pixels across 360 degrees,
    so anything finer than this cannot be seen at that zoom however carefully
    it is sent.
    """
    try:
        zoom = float(zoom)
    except (TypeError, ValueError):
        zoom = 11.0
    return 360.0 / (256.0 * (2 ** max(zoom, 1.0))) * 1.5


def _feature(element, tolerance):
    tags = element.get("tags") or {}
    ways = []
    for member in element.get("members") or []:
        if member.get("type") != "way":
            continue
        if member.get("role") not in ("outer", "", None):
            continue
        points = [
            (round(node["lat"], 6), round(node["lon"], 6))
            for node in (member.get("geometry") or [])
            if node and node.get("lat") is not None and node.get("lon") is not None
        ]
        if len(points) >= 2:
            ways.append(points)
    if not ways:
        return None

    rings = []
    before = after = 0
    for ring in _stitch(ways):
        before += len(ring["points"])
        thinned = simplify(ring["points"], tolerance)
        after += len(thinned)
        if len(thinned) < 2:
            continue
        rings.append({"closed": ring["closed"], "points": [list(p) for p in thinned]})
    if not rings:
        return None

    level = str(tags.get("admin_level") or "")
    return {
        "id": element.get("id"),
        "name": tags.get("name:en") or tags.get("name") or "Unnamed area",
        "local_name": tags.get("name"),
        "admin_level": level,
        "level_label": LEVEL_NAMES.get(level, f"Level {level}" if level else "Area"),
        "rings": rings,
        "points_before": before,
        "points_after": after,
    }


def boundaries(south, west, north, east, zoom):
    """Administrative outlines over one viewport.

    The box is snapped out to a grid cell first, so this is the same question
    for every viewport inside that cell and the cache can answer it.
    """
    for name, value in (
        ("south", south), ("west", west), ("north", north), ("east", east)
    ):
        try:
            if value is None or not math.isfinite(float(value)):
                raise ValueError
        except (TypeError, ValueError):
            raise UpstreamError(f"The map area is missing its {name} edge.") from None
    south, west, north, east = float(south), float(west), float(north), float(east)
    if south > north or west > east:
        raise UpstreamError(
            "The map area is inside out. Its south edge has to be below its "
            "north one."
        )

    snapped = snap_bbox(south, west, north, east, zoom)
    area = (snapped[2] - snapped[0]) * (snapped[3] - snapped[1])
    if area > MAX_BBOX_DEG2:
        raise UpstreamError(
            "This is too much of the map to draw boundaries for. Zoom in to a "
            "town or a district and they appear."
        )

    levels = levels_for_zoom(zoom)
    key = (
        f"bbox|{','.join(levels)}|"
        f"{snapped[0]:.4f},{snapped[1]:.4f},{snapped[2]:.4f},{snapped[3]:.4f}"
    )
    parsed, cached, stale = _ask_overpass(_query_for(*snapped, levels), key)

    tolerance = tolerance_for_zoom(zoom)
    features = []
    for element in parsed.get("elements") or []:
        if element.get("type") != "relation":
            continue
        feature = _feature(element, tolerance)
        if feature is not None:
            features.append(feature)
    # Numerically: "10" sorts before "5" as a string, and while nothing here
    # asks for level 10, LEVEL_NAMES knows about it and a sort that is only
    # accidentally right is a thing to trip over later.
    features.sort(key=lambda f: (int(f["admin_level"] or 0), f["name"]))

    return {
        "bbox": list(snapped),
        "requested_bbox": [south, west, north, east],
        "zoom": zoom,
        "levels": list(levels),
        "level_labels": [LEVEL_NAMES.get(level, level) for level in levels],
        "features": features,
        "cached": cached,
        "stale": stale,
    }


def _chain_query(lat, lon):
    timeout = int(float(_setting("overpass_timeout_seconds", 40)))
    return (
        f"[out:json][timeout:{timeout}];\n"
        f"is_in({lat:.6f},{lon:.6f})->.here;\n"
        f'area.here["boundary"="administrative"];\n'
        f"out tags;"
    )


def chain_at(lat, lon):
    """Which administrative areas one point sits inside, outermost first.

    Overpass's own `is_in`, asked for tags only, so it is a small answer and a
    fast one. This is what lets a click on the map say *which* taluka and
    *which* district: point-in-polygon done by the service that owns the
    polygons, rather than re-derived here from the simplified copy that was
    drawn -- which could disagree with it near an edge.
    """
    for name, value in (("latitude", lat), ("longitude", lon)):
        try:
            if value is None or not math.isfinite(float(value)):
                raise ValueError
        except (TypeError, ValueError):
            raise UpstreamError(f"The clicked point has no {name}.") from None
    lat, lon = float(lat), float(lon)
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise UpstreamError(f"There is no point at {lat}, {lon} on the earth.")

    parsed, cached, stale = _ask_overpass(
        _chain_query(lat, lon), f"at|{lat:.4f},{lon:.4f}"
    )

    areas = []
    for element in parsed.get("elements") or []:
        tags = element.get("tags") or {}
        level = str(tags.get("admin_level") or "")
        if level not in LEVEL_NAMES:
            continue
        areas.append({
            "id": element.get("id"),
            "name": tags.get("name:en") or tags.get("name") or "Unnamed area",
            "local_name": tags.get("name"),
            "admin_level": level,
            "level_label": LEVEL_NAMES[level],
        })
    areas.sort(key=lambda area: int(area["admin_level"]))
    return {"lat": lat, "lon": lon, "areas": areas, "cached": cached, "stale": stale}
