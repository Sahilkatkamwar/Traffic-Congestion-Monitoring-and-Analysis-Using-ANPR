"""Follow: a live watch for one vehicle to be seen again.

Trace (P4d) is a search over what has already been written. Follow is the
opposite direction in time: a target is named now and every sighting committed
after that is checked against it, so a vehicle appearing on a second camera
reaches the screen without anybody searching again.

Three things follow from that and none of them is a preference:

  * **The state is per websocket connection and it is in memory.** Two people
    watching Live follow different vehicles, a follow is a session rather than
    a record, and there is no row anywhere that has to be cleaned up when a
    browser is closed. A disconnect drops the set with the connection.
  * **Matching is `matching.py`'s, never string equality.** The whole reason
    Follow is useful is that the second camera reads the plate differently --
    which is the same reason Trace is fuzzy. Both sides of the comparison are
    OCR output here, so both offer their voted read, their raw read and their
    stored candidates, and a match through a candidate carries the same small
    penalty the search spends.
  * **A session that stops says so.** Nothing here silently expires: a target
    that has not been seen for `follow_timeout_seconds` ends with a reason the
    screen can render, because a live watch that quietly stopped watching is
    worse than one that was never started.

Nothing in this module writes. It reads a sighting the writer has already
committed and answers a question about it.
"""

import itertools
import time

from app import matching

# Same floor the Trace search opens on. A follow is the same question asked
# forwards, so it is answered at the same confidence.
DEFAULT_MIN_SCORE = 0.72
DEFAULT_TIMEOUT_SEC = 120.0

_ids = itertools.count(1)


def target_forms(row):
    """Every string this sighting offers to be followed by.

    `matching.plate_forms` is the public name for what the search scores
    against: the voted plate, the raw read, and the stored candidates, the last
    marked as the weaker evidence they are.
    """
    return matching.plate_forms(row)


def score(forms, row):
    """Best (score, matched text, how) between a follow target and a sighting.

    Both sides are reads, so both sides offer candidates, and the candidate
    penalty applies if either side matched through one -- a candidate against a
    candidate is not stronger evidence than a candidate against a voted plate.
    """
    best = (0.0, None, None)
    voted = matching.normalize(row["plate_text"] or "")
    for text, seen_as_candidate in matching.plate_forms(row):
        for target, target_is_candidate in forms:
            value = matching.similarity(target, text)
            weak = seen_as_candidate or target_is_candidate
            if weak:
                value -= matching.CANDIDATE_PENALTY
            if value > best[0]:
                if weak:
                    how = "candidate"
                else:
                    how = "plate" if text == voted else "raw"
                best = (value, text, how)
    return best


class FollowSet:
    """Everything one websocket connection is following.

    Owned by the connection's handler and touched only from the event loop, so
    there is no lock here: the writer thread never reaches this object -- it
    publishes a committed row to the hub and this asks a question about it on
    the way out.
    """

    def __init__(self, timeout_sec=DEFAULT_TIMEOUT_SEC, min_score=DEFAULT_MIN_SCORE):
        self.timeout_sec = float(timeout_sec)
        self.min_score = float(min_score)
        self.sessions = {}

    # -- lifecycle ------------------------------------------------------------

    def start(self, row, now=None):
        """Begin following the vehicle in `row`. Returns the session.

        A sighting with no plate at all cannot be followed and the caller is
        told why rather than being handed a session that can never match.
        """
        forms = target_forms(row)
        if not forms:
            raise ValueError(
                "This vehicle has no plate read, so there is nothing to follow it by."
            )
        now = time.monotonic() if now is None else now
        follow_id = f"f{next(_ids)}"
        session = {
            "follow_id": follow_id,
            "sighting_id": row["sighting_id"],
            "source_id": row["source_id"],
            "plate_text": row["plate_text"],
            "plate_conf": row["plate_conf"],
            "plate_crop_path": row["plate_crop_path"],
            "vehicle_type": row["vehicle_type"],
            "started_ts": row["last_seen_ts"],
            "matches": 0,
            "min_score": self.min_score,
            "timeout_sec": self.timeout_sec,
            # Not sent to the browser: monotonic seconds are meaningless there
            # and the wall clock is what the screen shows.
            "_forms": forms,
            "_started": now,
            "_last_seen": now,
        }
        self.sessions[follow_id] = session
        return session

    def stop(self, follow_id):
        return self.sessions.pop(follow_id, None)

    def clear(self):
        """A disconnect ends every session this connection held."""
        self.sessions.clear()

    def active(self):
        return list(self.sessions.values())

    # -- the two questions asked of it ---------------------------------------

    def match(self, row, now=None):
        """Every session this committed sighting is an update for.

        Returns [(session, score, matched text, how)]. A session that matches
        has its clock reset, which is what makes the timeout mean "not seen"
        rather than "started a while ago".
        """
        now = time.monotonic() if now is None else now
        hits = []
        for session in list(self.sessions.values()):
            value, text, how = score(session["_forms"], row)
            if value >= self.min_score:
                session["_last_seen"] = now
                session["matches"] += 1
                hits.append((session, round(value, 4), text, how))
        return hits

    def expired(self, now=None):
        """Sessions that have gone `timeout_sec` with no match, removed."""
        now = time.monotonic() if now is None else now
        gone = []
        for follow_id, session in list(self.sessions.items()):
            if now - session["_last_seen"] >= self.timeout_sec:
                gone.append(self.sessions.pop(follow_id))
        return gone

    def next_deadline(self, now=None):
        """Seconds until the earliest session expires, or None if empty.

        The websocket's wait is shortened to this, so "no longer visible"
        appears when it is true rather than on the next keepalive tick.
        """
        if not self.sessions:
            return None
        now = time.monotonic() if now is None else now
        return max(
            0.0,
            min(
                session["_last_seen"] + self.timeout_sec - now
                for session in self.sessions.values()
            ),
        )


def public(session):
    """The session as the browser sees it: no monotonic clocks, no forms."""
    return {k: v for k, v in session.items() if not k.startswith("_")}
