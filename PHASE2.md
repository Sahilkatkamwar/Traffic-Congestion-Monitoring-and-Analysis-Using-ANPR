# PHASE2.md

Build spec for the next phase of the ANPR platform. Extends `CLAUDE.md` (P0–P5,
now shipped) — everything in that file that isn't overridden below still
applies: environment, Windows rules, hardware constraints, banned dependencies,
one-writer SQLite, visual direction, interface rules, working rules.

Do not re-open P0–P5. Do not modify `app/detect.py`, `app/ocr.py`,
`app/grammar.py`, `app/matching.py`'s scoring, `app/stitch.py`'s adoption or
footprint logic, or any model weight to build anything in this file. Every
phase below is additive.

---

## Declined, on purpose

**No automatic internet lookup of real vehicle-owner personal data.**
P11 below implements a *registry* — a table you populate yourself (directly or
by CSV import) — not a scraper or a third-party "plate to owner" data-broker
API. Do not add one, even at a "free tier," even if asked again. A plate with
no registry entry shows nothing extra; it never fabricates or fetches an
answer from the internet. If a licensed Vahan/NIC integration is obtained
later, it becomes a second data source behind the same lookup call — that is
a separate, explicitly-approved piece of work, not something to infer from
this file.

---

## New credentials

Same pattern as `TEXTBEE_*` in CLAUDE.md: environment only, never in
`config/settings.yaml`, never returned by any API response (name only).

| var | purpose |
|---|---|
| `MAPTILER_API_KEY` | map tiles, P10 |

If `MAPTILER_API_KEY` is unset, the map must still work — fall back to plain
OpenStreetMap raster tiles rather than failing to render. Same
degrade-don't-break pattern as the P0–P5 webcam check.

---

## Data contract additions — FROZEN once shipped

Do not change these field names or types without asking, same rule as the
original contract. Do not touch the P0–P5 tables to build any of this.

### `analyze_runs` (new)
| field | type | notes |
|---|---|---|
| run_id | int PK | |
| kind | text | `image` / `video` |
| original_filename | text | |
| status | text | `queued` / `processing` / `done` / `error` |
| progress | real | 0–1 for video, null for image |
| error | text | nullable |
| created_ts | timestamp | |
| thumbnail_path | text | |
| result_path | text | saved JSON/CSV export, reloaded on selection |

### `vehicle_registry` (new)
| field | type | notes |
|---|---|---|
| plate_text | text PK | normalized the same way `blacklist` normalizes |
| owner_name | text | nullable |
| vehicle_model | text | nullable |
| vehicle_color | text | nullable |
| tag | text | e.g. `resident` / `staff` / `fleet` / `visitor` / `unknown` |
| contact | text | nullable |
| notes | text | nullable |
| added_ts | timestamp | |
| updated_ts | timestamp | |

### `traffic_levels` (new)
| field | type | notes |
|---|---|---|
| source_id | text FK | |
| window_start_ts | timestamp | |
| window_end_ts | timestamp | |
| count | int | sightings in window |
| level | text | `low` / `medium` / `high` |
| computed_ts | timestamp | |

### `history_events` (new, append-only)
| field | type | notes |
|---|---|---|
| event_id | int PK | |
| event_type | text | `sighting` / `alert` / `notification_sent` / `source_add` / `source_remove` / `source_error` / `blacklist_edit` / `registry_edit` / `analyze_run` / `follow_session` |
| ts | timestamp | |
| summary | text | one line, human-readable, same spirit as `alerts.detail` |
| ref_table | text | nullable, for jump-to-record in the UI |
| ref_id | text | nullable |
| detail | json | nullable |

All four tables go through the existing single writer queue. No second writer,
same rule as CLAUDE.md.

---

## Phases

One phase per session, same as before. Announce the phase before writing code.
Do not start the next until exit criteria pass.

### P6 — Analyze: persisted, multi-session, deletable

Currently an Analyze run's result exists only in the response and is lost on
navigation. Change that:

- Every Analyze run (image or video) writes an `analyze_runs` row instead of
  being thrown away after rendering.
- A left rail / dropdown lists past runs: thumbnail, filename, kind,
  timestamp, status.
- Selecting a run loads its saved annotated result (boxes, plate reads, crops)
  without re-running detection.
- Multiple runs can be mid-processing at once; each shows its own progress bar
  independent of the others.
- Each run has a delete (trash) icon. One confirm, then the DB row, its crops,
  and its result export are removed. Deleting a run that's still processing
  must stop that job cleanly first — see Known traps.
- Still works with zero cameras configured (existing P4c rule holds).

**Exit:** upload two different files back-to-back without navigating away;
both remain selectable and viewable independently; deleting one leaves the
other's files and DB rows untouched.

### P7 — Alerts: manual send

- The Control-room panel (Alerts screen) gets a **Send test alert** button next
  to the saved number.
- It composes and sends one real message through the existing
  `app/notify.py` / TextBee path, tagged `kind: manual_test` so it's
  distinguishable from `blacklist`-triggered alerts in the log and in P13's
  History.
- Button disables while sending, shows the result inline (sent / the specific
  failure reason), and does not require a real sighting or blacklist match to
  exist.
- Every existing guarantee from CLAUDE.md's notify section still holds:
  non-blocking dispatch on a daemon thread, retried on failure, credentials
  environment-only and never echoed back to the client.

**Exit:** clicking the button with a number saved dispatches one real message
through the TextBee gateway and the UI reflects success or the specific
failure reason.

### P8 — Sources: three phone-camera quick-add slots

- Sources screen gains a **Phone cameras** panel with exactly three labeled
  slots (Phone 1 / Phone 2 / Phone 3), each a single IP Webcam URL field
  (e.g. `http://<ip>:8080/video`).
- Saving a slot runs through the *same* connection-test, preview-frame, and
  map-placement flow the general Live-camera add flow already has. This is UI
  convenience for testing, not a second code path — nothing about the source
  abstraction changes.
- The general Live-camera add flow stays available for anything beyond the
  three slots, or for non-IP-Webcam sources.

**Exit:** entering three different `http://<ip>:8080/video` URLs and saving
starts three independent workers, all visible on Live and on the camera wall
simultaneously.

### P9 — Live: plate detail on click, browser geolocation, live "Follow"

Three pieces, one phase.

**1. Plate detail on click, everywhere on Live.** Clicking any live sighting —
map marker, feed item, *or* a camera-wall detection box — opens the same
evidence panel: plate string, confidence, vehicle type, source, timestamp,
crop. The camera wall wasn't wired to this before; the map and feed already
were. No new data is needed for this part.

**2. Browser geolocation.** Opening Live prompts for `navigator.geolocation`
permission and shows a "you are here" marker, styled distinctly from source
markers. This is a map convenience only — it never affects any timestamp,
sighting, camera placement, or the timestamp rule from CLAUDE.md. If
permission is denied, the map behaves exactly as before; no error state blocks
the screen.

**3. Follow mode (new).** A **Follow** toggle sits next to the existing "Trace
this vehicle" action on every live sighting. Follow is a *live* session,
distinct from Trace's after-the-fact search over historical sightings:

- The server keeps an in-memory follow-set — `{plate_candidates, started_ts}`
  per websocket connection — never persisted, cleared on disconnect.
- Every new sighting is checked against active follow targets using the exact
  same fuzzy matcher `matching.py` already uses for Trace. Never exact string
  equality, same rule as everywhere else plates are compared.
- A match pushes a `follow_update` event over the existing websocket; the
  client extends a live trail on the map and pulses the marker per the
  existing 150–250ms spring-eased motion spec. Nothing else animates, same
  rule as before.
- A follow session ends when the user stops it, or after
  `follow_timeout_seconds` (config, default 120) with no matching sighting —
  shown in the UI as "no longer visible," never a silent stop.

**Exit:** starting Follow on a sighting from one camera, then the same vehicle
appearing on a second connected camera, updates the map path live with no page
reload or manual re-search.

### P10 — Map: MapTiler tiles + free administrative boundaries

- Base tile layer switches to MapTiler (`MAPTILER_API_KEY`), giving
  street/locality-level zoom detail.
- A toggleable **Boundaries** overlay fetches administrative polygons — state →
  district → taluka/tehsil → village/nagar — for the current viewport from
  OpenStreetMap's Overpass API. Free, keyless, no card. Cache results per
  viewport bounding box; do not refetch on every pan/zoom tick, or the public
  Overpass instance's rate limit gets hit mid-demo.
- Falls back to plain OpenStreetMap raster tiles if `MAPTILER_API_KEY` is
  unset (see credentials section above).

**Exit:** zooming in on any Indian location shows street-level MapTiler
detail; toggling Boundaries draws the taluka/district outline the clicked
point sits in, labelled.

### P11 — Vehicle registry (local data only — read "Declined, on purpose" first)

- `vehicle_registry` table, edited from a new Registry panel (Sources screen or
  its own small section): add / edit / remove by plate, same
  normalize-then-fuzzy-match pattern `blacklist` already uses.
- CSV import so a real fleet, resident, or staff list can be loaded in one
  action rather than row by row. A file that doesn't parse is refused,
  nothing is changed — same rule the blacklist and settings writers already
  follow.
- Lookup is automatic and instant, everywhere a plate is already shown — Live
  evidence panel, Recorded/Analyze results, Trace. If the plate fuzzy-matches
  a registry row, `owner_name` / `vehicle_model` / `tag` / `notes` render
  inline in that same panel. No extra click beyond what already opens
  evidence.
- No plate is ever sent to any external service for this feature.

**Exit:** a plate added to the registry (directly or via CSV) shows its
registry details automatically the next time that plate is read anywhere in
the app; a plate with no entry shows nothing extra.

### P12 — Insights: low/medium/high traffic level, and a hook for signal control

- Rolling count of sightings per source over a configurable window
  (`traffic_window_seconds`, default 60), bucketed into low/medium/high
  against configurable thresholds (`traffic_thresholds: {medium: N, high: M}`,
  per-source override with a global default).
- Shown as a colored badge on Insights, and optionally on each camera-wall
  tile, plus a small time-series chart of level over the day.
- `traffic_levels` rows are written on the existing writer queue and exposed
  at `GET /api/traffic/current` (all sources) and
  `GET /api/traffic/current/<source_id>`. Nothing consumes this endpoint yet —
  it exists so a future signal-control integration is a client of this API,
  not a rebuild of this pipeline.

**Exit:** a burst of sightings on one source flips its badge low → high within
one polling/websocket tick, and eases back down once the window passes with no
new sightings.

### P13 — History tab

- New top-level nav item, own screen. Append-only activity log across the
  whole app: sightings written, alerts raised, notifications sent (including
  P7's manual ones), sources added/removed/errored, blacklist edits, registry
  edits, analyze runs started/finished/deleted, follow sessions started/ended.
- Every existing write path that should log one gets a single
  `log_event(...)` call added at the point the action already happens — no new
  business logic, no re-deciding anything that already happened.
- UI: reverse-chronological, filter chips by event type and date range,
  free-text search over `summary`, click a row to jump to the relevant screen
  (an alert row opens Alerts, a sighting row opens its evidence, etc.).

**Exit:** performing one action from each event type produces one new History
row, visible live via the existing websocket, no manual refresh needed.

---

## Known traps for this pass

- Follow-mode state is per websocket connection, in memory. A reconnect must
  clear stale follow targets rather than leaking them forever.
- Overpass and MapTiler are both rate-limited free services. Cache boundary
  polygons per viewport and don't refetch on every pan tick.
- Deleting an `analyze_run` mid-processing must stop that specific job cleanly
  (same supervision pattern used to stop a source worker) before removing its
  files — otherwise it's the Windows "dead handle locks the video file" trap
  from CLAUDE.md, wearing a new hat.
- CSV import for the registry needs the same "refuse a file that doesn't
  parse, change nothing" rule the blacklist and settings writers already use.
- Registry and blacklist matching both go through `matching.py`'s fuzzy path —
  a plate one character off from a registry or blacklist entry should still
  surface it.
- Traffic-level thresholds need real tuning once real cameras are live. Ship a
  sane default as a config value, not a constant — same "simplest option that
  satisfies the exit criteria, note the assumption" rule as CLAUDE.md.
- `navigator.geolocation` can be denied or unavailable (no HTTPS, no
  permission). The Live screen must render fully either way.

---

## Working rules (unchanged from CLAUDE.md)

Announce the phase before writing code. One phase per session; don't start the
next until exit criteria pass. Do not add dependencies or run pip beyond
what's already installed. No fields on any table — P0–P5's or the four new
ones above — without asking first, once that table has shipped.