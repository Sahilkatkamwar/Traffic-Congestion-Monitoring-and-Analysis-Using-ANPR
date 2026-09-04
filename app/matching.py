"""Finding a vehicle when nobody knows exactly what its plate says.

Exact string equality is never used here, and that is not a style preference.
The same vehicle passing two cameras produces two different strings -- one
camera saw MH15HY2237 and the other saw MH15HY22 because the last two digits
were behind a scooter -- so an equality test answers "no such vehicle" about a
vehicle that is sitting in the database twice.

Three things make the search work:

  * **Confusion-weighted edit distance.** A substitution between glyphs the OCR
    model actually confuses costs a fraction of a normal one. MH15HY2237 against
    MHI5HY2237 is one cheap edit, near-certainly the same plate; against
    MH15KY2237 it is one full edit, plausibly a different vehicle. Plain
    Levenshtein calls those two the same, which is exactly the mistake that
    puts the wrong vehicle in front of an investigator.
  * **Candidates, not just the winner.** Every sighting carries the top-k
    alternatives from the vote, and a plate whose primary read lost by one
    character is often correct in second place. Searching those as well is what
    the plate_candidates column is for.
  * **A ranked list, never one answer.** Every result carries its score and how
    it matched. A fuzzy search that silently returns one row is asserting a
    certainty it does not have.

Retrieval is two-stage, because scoring every plate in the database against
every query stops being free once there are more than a few thousand. Stage one
is cheap and generous -- a length window in SQL, then a skeleton bigram overlap
in memory. Stage two is the weighted distance, on what survives.
"""

import json

from app.grammar import CONFUSED_WITH, correct, normalize

# Edit costs. A gap and an unrelated substitution are the usual 1.0; a swap
# between confusable glyphs is cheap, because that is the edit OCR makes.
_GAP = 1.0
_SUB = 1.0
_CONFUSED_SUB = 0.35

# A match found in the top-k alternatives rather than in the voted answer is
# real evidence but weaker evidence, and two sightings that tie on distance
# should not tie on rank. Small on purpose: it breaks ties, it does not decide
# matches.
_CANDIDATE_PENALTY = 0.03

# Stage-one width. Reads of the same plate differ mostly by dropped characters,
# and three is already generous for a ten-character plate.
_LENGTH_WINDOW = 3

# character -> the one character standing for its whole confusion group
_SKELETON = {}
for _char, _group in CONFUSED_WITH.items():
    _SKELETON[_char] = min(_group)


def skeleton(text):
    """Collapse a plate onto its confusion groups.

    MH15HY2237 and MHI5HY2237 have the same skeleton, so two reads of one plate
    survive the prefilter together even when they share no exact substring.
    """
    return "".join(_SKELETON.get(char, char) for char in text)


def _bigrams(text):
    return {text[i : i + 2] for i in range(len(text) - 1)} or {text}


# ------------------------------------------------------------------ distance


def distance(a, b):
    """Confusion-weighted Levenshtein distance between two plate strings.

    Not an integer: a confusable substitution costs 0.35, so three of them stay
    cheaper than one genuine character difference.
    """
    a, b = normalize(a), normalize(b)
    if a == b:
        return 0.0
    if not a or not b:
        return float(len(a) + len(b))

    previous = [j * _GAP for j in range(len(b) + 1)]
    for i, char_a in enumerate(a, start=1):
        current = [i * _GAP]
        confusable = CONFUSED_WITH.get(char_a, ())
        for j, char_b in enumerate(b, start=1):
            if char_a == char_b:
                substitute = 0.0
            elif char_b in confusable:
                substitute = _CONFUSED_SUB
            else:
                substitute = _SUB
            current.append(
                min(
                    previous[j] + _GAP,
                    current[j - 1] + _GAP,
                    previous[j - 1] + substitute,
                )
            )
        previous = current
    return previous[-1]


def similarity(a, b):
    """Distance as a 0-1 score, 1 being the same plate.

    Normalised by the longer string, so a short read matching the front of a
    long one scores honestly rather than perfectly.
    """
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    return max(0.0, 1.0 - distance(a, b) / longest)


# ----------------------------------------------------------------- retrieval


def _query_forms(query):
    """The query as read, and as the grammar would correct it.

    Stored plates are corrected, so a query typed the way OCR would have got it
    wrong still needs to reach them -- and a query typed correctly needs to
    reach the sightings the grammar could not fix.
    """
    forms = []
    raw = normalize(query)
    if raw:
        forms.append(raw)
    fixed = correct(raw)["text"]
    if fixed and fixed not in forms:
        forms.append(fixed)
    return forms


def _stored_forms(row):
    """Every string one sighting offers to be matched against.

    Returns [(text, is_candidate)]. The voted answer and the raw read come
    first; the alternatives follow and are marked, because they are weaker
    evidence and carry a penalty.
    """
    forms = []
    seen = set()
    for key in ("plate_text", "plate_raw"):
        text = normalize(row[key] if row[key] is not None else "")
        if text and text not in seen:
            seen.add(text)
            forms.append((text, False))

    packed = row["plate_candidates"]
    if packed:
        try:
            for candidate in json.loads(packed):
                text = normalize(candidate.get("text", ""))
                if text and text not in seen:
                    seen.add(text)
                    forms.append((text, True))
        except (ValueError, TypeError, AttributeError):
            # A malformed candidates blob must not sink the search. The voted
            # plate is still there and still matchable.
            pass
    return forms


# Public name for the same thing. P5's alerts match a blacklist entry against
# every string a sighting offers, exactly as the search does, and importing a
# module-private helper across the package would be borrowing rather than an
# interface.
plate_forms = _stored_forms


def _score_row(row, forms):
    """Best (score, matched text, how) for one sighting against the query.

    `how` is 'plate', 'raw' or 'candidate' -- the UI shows how a fuzzy result
    matched, because a match through a third-choice alternative deserves to
    look different from an exact hit on the voted plate.
    """
    best = (0.0, None, None)
    for text, is_candidate in _stored_forms(row):
        for form in forms:
            score = similarity(form, text)
            if is_candidate:
                score -= _CANDIDATE_PENALTY
            if score > best[0]:
                if is_candidate:
                    how = "candidate"
                else:
                    how = "plate" if text == normalize(row["plate_text"] or "") else "raw"
                best = (score, text, how)
    return best


def _rows(conn, query, window=_LENGTH_WINDOW):
    """Stage one: plates worth scoring at all.

    A length window in SQL, then a skeleton bigram in memory. Both are lossy in
    principle and neither is lossy in practice at the scores this search
    reports: two ten-character plates sharing no confusion-group bigram are
    nowhere near each other.
    """
    length = len(normalize(query))
    rows = conn.execute(
        "SELECT sighting_id, source_id, plate_text, plate_raw, plate_conf, "
        "       plate_candidates, vehicle_type, first_seen_ts, last_seen_ts, "
        "       crop_path, plate_crop_path, frames_voted "
        "FROM sightings "
        "WHERE plate_text IS NOT NULL "
        "  AND LENGTH(plate_text) BETWEEN ? AND ?",
        (max(1, length - window), length + window),
    ).fetchall()

    wanted = set()
    for form in _query_forms(query):
        wanted |= _bigrams(skeleton(form))

    kept = []
    for row in rows:
        marks = set()
        for text, _ in _stored_forms(row):
            marks |= _bigrams(skeleton(text))
        if marks & wanted:
            kept.append(row)
    return kept


# -------------------------------------------------------------------- search


def match_sightings(conn, query, min_score=0.72):
    """Every sighting that could be this plate, best first.

    One row per sighting, each carrying its own score. This is the raw material:
    `search` groups it into the candidate list, `trajectory` orders it by time.
    """
    forms = _query_forms(query)
    if not forms:
        return []

    matches = []
    for row in _rows(conn, query):
        score, text, how = _score_row(row, forms)
        if score >= min_score:
            item = dict(row)
            item["score"] = round(score, 4)
            item["matched_text"] = text
            item["matched_via"] = how
            matches.append(item)
    matches.sort(key=lambda item: (-item["score"], item["first_seen_ts"] or ""))
    return matches


def search(conn, query, limit=10, min_score=0.72):
    """Ranked candidate plates for the Trace screen.

    Grouped by the stored plate string, not by vehicle: two reads of one vehicle
    that disagree stay two rows, each with its own count and score. Collapsing
    them here would mean deciding they are the same vehicle silently, which is
    the decision the user is being asked to make. Picking either one and opening
    its trajectory gathers both, because `trajectory` matches fuzzily too.
    """
    grouped = {}
    for match in match_sightings(conn, query, min_score):
        plate = match["plate_text"]
        entry = grouped.get(plate)
        if entry is None:
            entry = grouped[plate] = {
                "plate_text": plate,
                "score": match["score"],
                "matched_text": match["matched_text"],
                "matched_via": match["matched_via"],
                "sighting_count": 0,
                "sighting_ids": [],
                "sources": [],
                "first_seen_ts": match["first_seen_ts"],
                "last_seen_ts": match["last_seen_ts"],
                "plate_conf": match["plate_conf"],
                "plate_crop_path": match["plate_crop_path"],
            }
        entry["sighting_count"] += 1
        entry["sighting_ids"].append(match["sighting_id"])
        if match["source_id"] not in entry["sources"]:
            entry["sources"].append(match["source_id"])
        if match["score"] > entry["score"]:
            entry.update(
                score=match["score"],
                matched_text=match["matched_text"],
                matched_via=match["matched_via"],
            )
        if (match["first_seen_ts"] or "") < (entry["first_seen_ts"] or ""):
            entry["first_seen_ts"] = match["first_seen_ts"]
        if (match["last_seen_ts"] or "") > (entry["last_seen_ts"] or ""):
            entry["last_seen_ts"] = match["last_seen_ts"]
        # Show the clearest evidence for the group, not the first row of it.
        if (match["plate_conf"] or 0) > (entry["plate_conf"] or 0):
            entry["plate_conf"] = match["plate_conf"]
            if match["plate_crop_path"]:
                entry["plate_crop_path"] = match["plate_crop_path"]

    results = sorted(
        grouped.values(),
        key=lambda entry: (-entry["score"], -entry["sighting_count"], entry["plate_text"]),
    )
    return results[:limit]
