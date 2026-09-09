"""One SMS to one control room, when a blacklisted plate is actually seen.

This is a notification, not a record. The alert in the database is the record --
it is written by the writer inside the commit path and it is what the Alerts
screen reads. This module exists because somebody who is not looking at the
screen needs to know, and the only thing it is allowed to do on failure is
write a log line.

## What it must never do

**It must never run on the writer thread.** An HTTP request to a carrier takes
between 200 ms and a timeout, and the writer is the only thing in this app that
writes to SQLite. A blocked writer is every source stalled. So `send_alert()`
puts a message on a queue and returns; a daemon thread does the network.

**It must never send twice for one alert.** Two guards, at different levels.
`app/alerts.py:record` already refuses to insert an alert whose
(kind, sighting_ids) is already there, so a re-emitted track cannot produce a
second alert row at all -- and only a row `record` actually inserted is ever
offered here. On top of that this module keeps the alert_ids it has accepted
and refuses a repeat, because the cost of the second guard is a set and the
cost of being wrong is a control room's phone at 3 a.m.

**It must never backfill.** Nothing in here reads the alerts table. The only
way a message is sent is a sighting committing now and raising an alert now.
Turning the feature on does not notify anybody about yesterday.

## The manual test

`send_test()` sends one message because somebody pressed a button, not because
anything was seen. It is the same queue, the same daemon thread, the same
retries and the same number -- the point of it is to prove that path reaches
the phone, so a second path would prove nothing. Two things keep it apart from
a real alert: it is tagged `manual_test` in the job, in the log line and in
what the API returns, and its text says on its first line that it is not an
alert. It raises nothing, writes no row, and never touches `_last`, so the
"last notification" the Alerts screen reports still means the last *alert*.

## The number is set from the screen; credentials never are

`config/settings.yaml` carries the control-room number and which provider to
use -- neither is a secret -- and it carries no credential. `set_police_number`
rewrites that one line from the Alerts screen, so the number survives a
restart with nothing to edit by hand, and it is the number used from the next
alert onward. The API key and the device id are read from the environment at
send time and are never returned to a browser, so a checkout of this repo
cannot send anything and rotating a key needs no edit to a tracked file.

## Providers

`textbee`  the real one, and it is a gateway rather than a carrier: the message
           is handed to the TextBee app on an Android phone you own, and that
           phone sends it on its own SIM and its own plan. There is therefore
           no sending number to configure -- the SIM in the device is the
           sender. A JSON POST with an `x-api-key` header, over urllib, because
           `requests` is not in requirements.txt and CLAUDE.md forbids pip.
`console`  prints the message it would have sent and records it. This is what
           the verification suite runs against -- it exercises the whole path
           from the committed sighting to the composed text without a gateway
           account and without sending anything to a real phone.
`none`     off.
"""

import json
import os
import queue
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from app import config
from app.db import from_iso, utc_now

# Sent messages beyond this are the carrier's problem: a long SMS is split into
# segments and billed per segment on the sending SIM. The composed alert is well
# under it; this is a guard against a `reason:` somebody pasted a paragraph into.
MAX_BODY_CHARS = 640

# One attempt is not enough for a control-room alert on a flaky link, and ten
# is a queue that never drains. Three, with a short backoff.
ATTEMPTS = 3
BACKOFF_SEC = (0.0, 2.0, 5.0)
HTTP_TIMEOUT_SEC = 15.0

TEXTBEE_URL = "https://api.textbee.dev/api/v1/gateway/devices/{device}/send-sms"

# TextBee sits behind Cloudflare, which refuses the stdlib's default
# `Python-urllib/3.x` outright -- HTTP 403 with a body of "error code: 1010",
# for a correct key and a real device. Measured: identical requests differing
# only in this header get 403 with the default and 200 with any of three
# ordinary ones. So this names the client honestly and the call works.
USER_AGENT = "anpr-city/1.0"

PROVIDERS = ("textbee", "console", "none")

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}

# What a queued message is for. The alert kind is the same word app/alerts.py
# writes into the alerts table, so a log line reads the same on both sides;
# `manual_test` is not a kind of alert and never reaches that table.
KIND_ALERT = "blacklist"
KIND_TEST = "manual_test"

# Manual tests kept for the screen to poll. A handful is all anybody needs --
# the button reports its own result and the log has the rest.
TEST_HISTORY = 20

# The environment variables this reads. Named here rather than inline so
# `describe()` can tell the UI exactly which one is missing.
ENV_API_KEY = "TEXTBEE_API_KEY"
ENV_DEVICE = "TEXTBEE_DEVICE_ID"
ENV_TO = "ANPR_POLICE_NUMBER"


def _notify_settings():
    return config.load_settings().get("notify") or {}


def _setting(key, fallback=None):
    value = _notify_settings().get(key)
    return fallback if value is None else value


def saved_number():
    """The number saved in the settings file, as it was typed."""
    value = _setting("police_number")
    if value is None:
        return None
    return str(value).strip() or None


def police_number():
    """The one number alerts go to. What was saved wins over the environment.

    One number, deliberately. A list is a distribution problem -- who is on it,
    who took it off, which of them is on shift -- and this app has no place to
    answer any of that. The control room forwards.

    The saved number is checked first, and that ordering is load-bearing: it is
    what somebody typed into the Alerts screen, and a number entered there that
    was then silently overridden would be the worst kind of wrong -- the screen
    would show one number and the SMS would go to another. The environment
    variable is what is left: a fallback for a deployment that has never set
    one from the browser.
    """
    saved = saved_number()
    if saved:
        return saved
    override = os.environ.get(ENV_TO, "").strip()
    return override or None


def clean_number(number):
    """E.164 as far as it can be checked without a carrier: +, then digits."""
    if not number:
        return None, "no number set"
    text = str(number).strip().replace(" ", "").replace("-", "")
    if not text.startswith("+"):
        return None, (
            f"{text} is not in international format. Write it as +<country "
            f"code><number>, for example +919876543210."
        )
    if not text[1:].isdigit() or not (8 <= len(text[1:]) <= 15):
        return None, f"{text} is not a phone number."
    return text, None


# ------------------------------------------------------- saving the number


class NumberEditError(Exception):
    """A refusal written for whoever is reading it on the screen."""


# One writer to the settings file, the same way app/alerts.py guards the
# blacklist. Two browser tabs saving at once is the ordinary case, not the
# exotic one.
_EDIT_LOCK = threading.Lock()


def _notify_block_bounds(lines):
    """(start, end) of the top-level `notify:` block, or (None, None).

    Line-based on purpose. Round-tripping the whole file through PyYAML would
    discard every comment in it, and config/settings.yaml is mostly comments --
    they are how the measurements behind each setting are recorded. Rewriting
    one line leaves the other several hundred byte for byte.
    """
    start = None
    for index, line in enumerate(lines):
        if start is None:
            if line.startswith("notify:"):
                start = index
            continue
        # The block ends at the next thing that starts in column 0 and is not a
        # comment or a blank line.
        if line[:1] not in (" ", "\t", "#", "\n", "\r", ""):
            return start, index
    return (start, len(lines)) if start is not None else (None, None)


def _rewrite_number(text, value):
    """The settings text with notify.police_number set to `value`.

    `value` is a validated number or None. A number is quoted: YAML 1.1 reads a
    bare +919876543210 as the integer 919876543210, which silently loses the
    plus and with it the country code.
    """
    shown = "null" if value is None else json.dumps(value)
    lines = text.splitlines(keepends=True)
    ending = "\r\n" if text.endswith("\r\n") else "\n"
    start, end = _notify_block_bounds(lines)

    if start is None:
        prefix = "" if not text or text.endswith("\n") else ending
        return f"{text}{prefix}{ending}notify:{ending}  police_number: {shown}{ending}"

    for index in range(start + 1, end):
        body = lines[index]
        if body.strip().startswith("#"):
            continue
        stripped = body.lstrip()
        if stripped.startswith("police_number:"):
            indent = body[: len(body) - len(stripped)]
            lines[index] = f"{indent}police_number: {shown}{ending}"
            return "".join(lines)

    lines.insert(start + 1, f"  police_number: {shown}{ending}")
    return "".join(lines)


def _write_atomic(path, text):
    """Replace the file in one step, so a reader never sees half of it.

    A temporary file beside it and then os.replace: on Windows that is the only
    rename that overwrites.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="", dir=str(path.parent),
        prefix=path.name + ".", suffix=".tmp", delete=False,
    )
    try:
        with handle:
            handle.write(text)
        os.replace(handle.name, path)
    except OSError:
        Path(handle.name).unlink(missing_ok=True)
        raise


def set_police_number(number, path=None):
    """Save the one number alerts are sent to. Returns it, or None if cleared.

    The settings file is the store, because it is already the store: the number
    was always `notify.police_number` and nothing else in the app had to change
    to read it from there. What is new is that this writes it, so it can be set
    from the browser and survive a restart.

    Validated before the write rather than after it, so a number that could
    never be sent to is refused with the reason on screen instead of being
    accepted and then quietly doing nothing on the next alert.
    """
    path = Path(path) if path is not None else config.settings_path()

    text = "" if number is None else str(number).strip()
    if text:
        cleaned, problem = clean_number(text)
        if cleaned is None:
            raise NumberEditError(problem)
    else:
        cleaned = None

    with _EDIT_LOCK:
        try:
            # newline="" on both the read and the write, so a file saved with
            # Windows line endings keeps them. Python's default translates
            # CRLF to LF on the way in and back on the way out, which would
            # rewrite every line of a file this is only allowed to change one
            # line of -- invisible in a diff of the content and very visible in
            # a diff of the bytes.
            original = (
                path.open(encoding="utf-8", newline="").read()
                if path.exists()
                else ""
            )
        except OSError as exc:
            raise NumberEditError(
                f"The settings file could not be read: {exc}."
            ) from exc

        if original:
            try:
                yaml.safe_load(original)
            except yaml.YAMLError as exc:
                # There is nothing safe to merge into, so nothing is written.
                raise NumberEditError(
                    f"The settings file does not parse ({exc}), so it cannot be "
                    f"changed safely. It has been left exactly as it was."
                ) from exc

        updated = _rewrite_number(original, cleaned)

        # Read back what is about to be written, before it is written. A
        # line-based edit that produced something PyYAML disagrees with would
        # take the whole app down on its next restart, and this is the one
        # place that can still catch it.
        try:
            parsed = yaml.safe_load(updated) or {}
        except yaml.YAMLError as exc:
            raise NumberEditError(
                f"Saving the number would have made the settings file "
                f"unreadable ({exc}), so nothing was changed."
            ) from exc
        landed = (parsed.get("notify") or {}).get("police_number")
        if (landed if landed is None else str(landed)) != cleaned:
            raise NumberEditError(
                "The number could not be saved: the settings file did not read "
                "back the way it was written, so nothing was changed."
            )

        _write_atomic(path, updated)

        # The loaded settings are a cached dict and the writer reads the number
        # out of it on every alert, so the save takes effect on the next alert
        # rather than on the next restart. Only this key is touched -- a full
        # reload would also undo any redirection a verification run has set up.
        settings = config.load_settings()
        block = settings.get("notify")
        if not isinstance(block, dict):
            block = {}
            settings["notify"] = block
        block["police_number"] = cleaned

    print(f"[notify] control-room number {'cleared' if cleaned is None else 'set to ' + cleaned}")
    return cleaned


# ------------------------------------------------------------------ message


def _clock(text):
    moment = from_iso(text)
    if moment is None:
        return text or "unknown"
    return moment.strftime("%Y-%m-%d %H:%M:%S UTC")


def compose(alert, sighting, source):
    """The text the control room receives.

    Every field the alert contract can supply about *what and where*, in the
    order somebody acting on it needs them: what to look for, how bad, what it
    is, which camera, when, and exactly where that camera is.

    A field that is genuinely unknown says so. It is never left out and never
    invented -- "camera not placed on the map" is actionable and a missing line
    is not.

    **Every value here comes from the alert and the sighting that raised it.**
    Nothing in this function has a default plate, a default camera or a default
    time, and nothing here is allowed to acquire one: a message that reads well
    with a placeholder in it is a message the control room cannot trust. What
    is unknown says "unknown" and says which field it is.

    `Plate:` is the read from THIS sighting -- `plate_text`, the voted and
    grammar-corrected string, falling back to `plate_raw` when correction
    produced nothing. The blacklisted registration is a separate line and only
    appears when it is a different string, which is the fuzzy-match case: the
    control room then sees both what the camera actually read and which watched
    registration it was matched to, and can disagree with the match.
    """
    read = sighting.get("plate_text") or sighting.get("plate_raw")
    listed = alert.get("plate_text")
    plate = read or listed or "unknown plate"
    severity = str(alert.get("severity") or "warning").upper()

    lat = source.get("lat")
    lon = source.get("lon")
    if lat is None or lon is None:
        where = "Location: camera not placed on the map"
    else:
        where = (
            f"Location: {float(lat):.5f}, {float(lon):.5f}\n"
            f"https://www.google.com/maps?q={float(lat):.5f},{float(lon):.5f}"
        )

    lines = [
        f"ANPR {severity}: blacklisted plate seen",
        f"Plate: {plate}",
    ]
    if listed and read and listed != read:
        # A fuzzy hit. app/alerts.py has already capped it at `warning` and its
        # `detail` says how far off it was; what the SMS has to carry is that
        # these are two different strings, so the read is not quietly presented
        # as the registration on the list.
        lines.append(f"Matches blacklisted: {listed}")
    lines += [
        f"Vehicle: {sighting.get('vehicle_type') or 'unknown'}",
        f"Camera: {source.get('name') or sighting.get('source_id') or 'unknown'}",
        f"Time: {_clock(sighting.get('first_seen_ts'))}",
        where,
        f"Reason: {alert.get('reason') or 'not given'}",
    ]
    body = "\n".join(lines)
    if len(body) <= MAX_BODY_CHARS:
        return body
    return body[: MAX_BODY_CHARS - 1] + "…"


def compose_test(clock=None):
    """The text a manual test sends, and it must not read like an alert.

    The phone this arrives on is the phone the real thing arrives on, so the
    first line says what it is before anybody has finished reading. There is no
    plate, no camera and no sighting time in it because there is no sighting --
    a test carrying a plate is a test somebody acts on.

    Nothing here is composed from the database. It is one fixed statement plus
    the clock, which is the only thing about a test that varies.
    """
    return "\n".join(
        [
            "ANPR TEST: this is not a real alert.",
            "Sent from the Alerts screen to check this number receives messages.",
            "No vehicle was seen and no alert was raised.",
            f"Time: {_clock(clock or utc_now())}",
        ]
    )


# ----------------------------------------------------------------- providers


def _reference(payload):
    """Whatever the gateway called this message, for the log line.

    The send is confirmed by the 2xx, not by this -- it is an identifier to
    quote when a message did not arrive, so an unrecognised response shape
    costs a nicer log line and never a false failure.
    """
    for holder in (payload.get("data"), payload):
        if isinstance(holder, dict):
            for key in ("smsBatchId", "_id", "id"):
                if holder.get(key):
                    return str(holder[key])
    return "accepted"


def _textbee(to, body):
    """POST one message to the Android phone that sends it. Raises on non-2xx.

    TextBee is a gateway, not a carrier. The API hands the text to the TextBee
    app on a phone you own and that phone sends it over its own SIM, on your
    own mobile plan -- so there is no sending number to configure here, because
    the SIM in the device is the sender. What it needs instead is which account
    (the API key) and which of that account's phones (the device id).
    """
    key = os.environ.get(ENV_API_KEY, "").strip()
    device = os.environ.get(ENV_DEVICE, "").strip()

    missing = [
        name
        for name, value in ((ENV_API_KEY, key), (ENV_DEVICE, device))
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"{' and '.join(missing)} not set in the environment. Set them and "
            f"restart the app -- they are read at send time, not from "
            f"config/settings.yaml, because they are credentials."
        )

    request = urllib.request.Request(
        TEXTBEE_URL.format(device=urllib.parse.quote(device)),
        data=json.dumps({"recipients": [to], "message": body}).encode("utf-8"),
        method="POST",
    )
    request.add_header("x-api-key", key)
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", USER_AGENT)

    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SEC) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        # TextBee says what is wrong in the body and it is worth reading:
        # "device not found" is a fixable sentence and "HTTP 404" is not.
        detail = ""
        try:
            raw = exc.read().decode("utf-8", "replace").strip()
        except Exception:  # noqa: BLE001 - the status is the useful part
            raw = ""
        try:
            parsed = json.loads(raw)
            said = parsed.get("message") or parsed.get("error") or raw
            detail = (
                "; ".join(str(x) for x in said)
                if isinstance(said, list)
                else str(said)
            )
        except Exception:  # noqa: BLE001 - Cloudflare answers in plain text
            # And it is worth keeping: a 403 whose body is "error code: 1010"
            # is Cloudflare refusing the client, not TextBee refusing the
            # message, and reading it is the difference between fixing the
            # request and rotating a key that was never wrong.
            detail = raw
        raise RuntimeError(
            f"TextBee refused the message ({exc.code})"
            + (f": {detail[:300]}" if detail else "")
        ) from exc
    return _reference(payload if isinstance(payload, dict) else {})


class _Console:
    """Prints instead of sending, and keeps what it printed.

    Not a mock bolted on for a test: it is how this path is verified without a
    carrier account, and it is what a deployment with no provider yet should be
    set to so the composed message is at least visible in the log.
    """

    def __init__(self):
        self.sent = []
        self._lock = threading.Lock()

    def __call__(self, to, body):
        with self._lock:
            self.sent.append({"to": to, "body": body, "ts": utc_now()})
            count = len(self.sent)
        print(f"[notify] console SMS to {to}:\n{body}")
        return f"console-{count}"


# ------------------------------------------------------------------ notifier


class Notifier:
    """Owns the queue, the worker thread and what the UI is told.

    One instance, held by the Pipeline, because the deduplication set has to be
    shared by everything that can raise an alert.
    """

    def __init__(self, transport=None):
        self.console = _Console()
        # An injection point for the verification suite, which needs to assert
        # on a failure path without a carrier that can be made to fail.
        self._transport = transport
        self._queue = queue.Queue()
        self._thread = None
        self._lock = threading.Lock()
        self._sent_ids = set()      # alert_ids accepted; never accepted twice
        self._sent = 0
        self._failed = 0
        self._skipped = 0
        self._last = None           # the last ALERT outcome, for the UI
        self._tests = []            # manual tests, oldest first, capped
        self._test_seq = 0
        self._test_sent = 0
        self._test_failed = 0

    # -- configuration ------------------------------------------------------

    def provider(self):
        name = str(_setting("provider", "none")).lower()
        return name if name in PROVIDERS else "none"

    def enabled(self):
        return bool(_setting("enabled", True)) and self.provider() != "none"

    def min_severity(self):
        name = str(_setting("min_severity", "info")).lower()
        return name if name in SEVERITY_RANK else "info"

    def readiness(self):
        """(ready, reason, detail). What the Alerts screen shows, and why.

        `reason` is the sentence the screen renders, so it is written for the
        person who set the number: it says whether alerts are going out and
        where to, and it names no file, no setting key and no environment
        variable. `detail` is the server-side half -- which credential the
        machine is missing -- and it is deliberately a separate field so that
        the screen can be plain while the log and the API stay actionable.
        """
        if not bool(_setting("enabled", True)):
            return False, "Sending is turned off.", "notify.enabled is false."
        name = self.provider()
        if name == "none":
            return False, (
                "No SMS service is set up on this server, so nothing is sent."
            ), (
                "notify.provider is `none`. Set it to `textbee` to send, or to "
                "`console` to print the message instead."
            )
        number, problem = clean_number(police_number())
        if number is None:
            if not police_number():
                return False, (
                    "No control-room number yet. Enter one below and new "
                    "blacklist alerts are sent to it."
                ), None
            return False, problem, None
        if name == "console":
            return True, (
                f"Alerts for {number} are written to the log rather than sent."
            ), "notify.provider is `console`."
        missing = [
            key
            for key in (ENV_API_KEY, ENV_DEVICE)
            if not os.environ.get(key, "").strip()
        ]
        if missing:
            return False, (
                f"SMS sending is not set up on this server, so nothing can be "
                f"sent to {number}."
            ), (
                f"{' and '.join(missing)} not set in the environment. Set them "
                f"and restart the app."
            )
        return True, f"New blacklist alerts are sent to {number}.", None

    def describe(self):
        ready, reason, detail = self.readiness()
        number, _ = clean_number(police_number())
        return {
            "provider": self.provider(),
            "enabled": self.enabled(),
            "police_number": number,
            "configured_number": police_number(),
            "saved_number": saved_number(),
            "min_severity": self.min_severity(),
            "ready": ready,
            "reason": reason,
            # The server-side half, for the log and for whoever is running the
            # machine. The Alerts screen renders `reason` and never this.
            "setup_detail": detail,
            "sent": self._sent,
            "failed": self._failed,
            "skipped": self._skipped,
            "last": dict(self._last) if self._last else None,
            # Manual tests are counted apart from alerts on purpose. "3 sent
            # this run" on the Alerts screen has to mean three alerts went out,
            # and a button somebody pressed twice must not be able to say it.
            "test_sent": self._test_sent,
            "test_failed": self._test_failed,
            "last_test": dict(self._tests[-1]) if self._tests else None,
            "env": {
                "api_key": ENV_API_KEY,
                "device_id": ENV_DEVICE,
                "number": ENV_TO,
            },
        }

    # -- sending ------------------------------------------------------------

    def send_alert(self, alert, sighting, source):
        """Queue one notification for one newly recorded alert.

        Called from the writer thread and returns immediately. Everything that
        can be decided without the network is decided here, so a message that
        is never going to be sent is counted and logged now rather than queued.
        """
        alert_id = alert.get("alert_id")
        with self._lock:
            if alert_id is not None and alert_id in self._sent_ids:
                # Belt and braces over record()'s own deduplication. If this
                # ever fires it is a bug upstream, and it says so.
                print(f"[notify] alert {alert_id} was already notified; not sending")
                return False
            if alert_id is not None:
                self._sent_ids.add(alert_id)

        if not self.enabled():
            self._skipped += 1
            return False
        wanted = SEVERITY_RANK[self.min_severity()]
        if SEVERITY_RANK.get(alert.get("severity"), 2) < wanted:
            self._skipped += 1
            return False

        ready, reason, detail = self.readiness()
        if not ready:
            self._skipped += 1
            self._last = {
                "ok": False,
                "alert_id": alert_id,
                "detail": reason,
                "ts": utc_now(),
            }
            # The log gets both halves: the sentence the screen shows, and the
            # server-side detail that says which credential is missing.
            print(
                f"[notify] not sent for alert {alert_id}: {reason}"
                + (f" ({detail})" if detail else "")
            )
            return False

        number, _ = clean_number(police_number())
        body = compose(alert, sighting, source)
        self._start()
        self._queue.put(
            {
                "kind": KIND_ALERT,
                "test_id": None,
                "alert_id": alert_id,
                "to": number,
                "body": body,
            }
        )
        return True

    def send_test(self, clock=None):
        """Send one message because somebody pressed a button. Never raises.

        Returns the record to watch: a dict with a `test_id`, a `status` of
        `sending` / `sent` / `failed` / `refused`, and -- once it is finished --
        either the gateway's reference or the reason it did not go. The screen
        polls that record rather than waiting on the response, because the
        network is on the daemon thread here exactly as it is for an alert.

        What it does NOT do matters as much as what it does. It writes no
        alert, reads no sighting and consults no blacklist: there is nothing to
        match and nothing to record, which is the whole point of being able to
        press it on a quiet system. It is also not gated on `min_severity` --
        that floor decides which alerts are worth waking somebody for, and a
        person pressing this button has already decided. Everything else that
        would stop a real alert going out stops this too, through the same
        `readiness()`, so a test that arrives proves an alert would.
        """
        with self._lock:
            live = next(
                (row for row in reversed(self._tests) if row["status"] == "sending"),
                None,
            )
            if live is not None:
                # A double click, or two screens open. Hand back the message
                # already in flight rather than sending a second one -- these
                # cost the sending SIM real money and a control room a real
                # interruption.
                return dict(live)
            self._test_seq += 1
            test_id = self._test_seq

        ready, reason, detail = self.readiness()
        if not ready:
            print(
                f"[notify] manual test {test_id} not sent: {reason}"
                + (f" ({detail})" if detail else "")
            )
            return self._remember(
                {
                    "test_id": test_id,
                    "kind": KIND_TEST,
                    "status": "refused",
                    "ok": False,
                    "to": None,
                    "detail": reason,
                    "reference": None,
                    "attempts": 0,
                    "created_ts": utc_now(),
                    "finished_ts": utc_now(),
                }
            )

        number, _ = clean_number(police_number())
        record = self._remember(
            {
                "test_id": test_id,
                "kind": KIND_TEST,
                "status": "sending",
                "ok": None,
                "to": number,
                "detail": None,
                "reference": None,
                "attempts": 0,
                "created_ts": utc_now(),
                "finished_ts": None,
            }
        )
        self._start()
        self._queue.put(
            {
                "kind": KIND_TEST,
                "test_id": test_id,
                "alert_id": None,
                "to": number,
                "body": compose_test(clock),
            }
        )
        return record

    def test_record(self, test_id):
        """One manual test as it now stands, or None. What the screen polls."""
        with self._lock:
            for row in self._tests:
                if row["test_id"] == test_id:
                    return dict(row)
        return None

    def _remember(self, record):
        with self._lock:
            self._tests.append(record)
            del self._tests[:-TEST_HISTORY]
            return dict(record)

    def _finish_test(self, outcome):
        with self._lock:
            for row in self._tests:
                if row["test_id"] == outcome.get("test_id"):
                    row.update(
                        status="sent" if outcome["ok"] else "failed",
                        ok=outcome["ok"],
                        detail=outcome.get("detail"),
                        reference=outcome.get("reference"),
                        attempts=outcome.get("attempts"),
                        finished_ts=outcome["ts"],
                    )
                    return

    def _start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._loop, name="notify", daemon=True
            )
            self._thread.start()

    def _loop(self):
        while True:
            try:
                job = self._queue.get(timeout=30.0)
            except queue.Empty:
                return
            if job is None:
                return
            try:
                self._deliver(job)
            except Exception as exc:  # noqa: BLE001 - a thread that dies is silent
                print(f"[notify] delivery crashed: {type(exc).__name__}: {exc}")
                if job.get("kind") == KIND_TEST:
                    # A test somebody is watching must never be left saying
                    # `sending` forever -- the screen would poll an answer that
                    # is never coming, and the next press would be refused as a
                    # duplicate of a message nothing is carrying.
                    self._record_outcome(
                        KIND_TEST,
                        {
                            "ok": False,
                            "kind": KIND_TEST,
                            "alert_id": None,
                            "test_id": job.get("test_id"),
                            "to": job.get("to"),
                            "detail": f"{type(exc).__name__}: {exc}",
                            "attempts": ATTEMPTS,
                            "ts": utc_now(),
                        },
                    )
            finally:
                self._queue.task_done()

    def _transport_for(self, name):
        if self._transport is not None:
            return self._transport
        return self.console if name == "console" else _textbee

    def _deliver(self, job):
        """One message, up to ATTEMPTS times. The same path for both kinds.

        An alert and a manual test are delivered by identical code, because a
        test that took a different route would prove nothing about the route an
        alert takes. All that differs is where the outcome is counted and what
        the log line calls it.
        """
        name = self.provider()
        send = self._transport_for(name)
        kind = job.get("kind", KIND_ALERT)
        label = (
            f"manual test {job['test_id']}"
            if kind == KIND_TEST
            else f"alert {job['alert_id']}"
        )
        error = None
        for attempt in range(ATTEMPTS):
            if attempt:
                time.sleep(BACKOFF_SEC[min(attempt, len(BACKOFF_SEC) - 1)])
            try:
                reference = send(job["to"], job["body"])
            except Exception as exc:  # noqa: BLE001 - reported, never raised on
                error = f"{type(exc).__name__}: {exc}"
                continue
            self._record_outcome(
                kind,
                {
                    "ok": True,
                    "kind": kind,
                    "alert_id": job.get("alert_id"),
                    "test_id": job.get("test_id"),
                    "to": job["to"],
                    "reference": reference,
                    "attempts": attempt + 1,
                    "ts": utc_now(),
                },
            )
            print(
                f"[notify] SMS sent to {job['to']} for {label} "
                f"({name}, reference {reference})"
            )
            return
        self._record_outcome(
            kind,
            {
                "ok": False,
                "kind": kind,
                "alert_id": job.get("alert_id"),
                "test_id": job.get("test_id"),
                "to": job["to"],
                "detail": error,
                "attempts": ATTEMPTS,
                "ts": utc_now(),
            },
        )
        # Loud, because a control-room notification that failed is the one
        # thing in this module somebody has to know about.
        print(f"[notify] SMS FAILED after {ATTEMPTS} attempts for {label}: {error}")

    def _record_outcome(self, kind, outcome):
        """Count it, and put it where whatever asked for it will look.

        A manual test never becomes `_last`. That field is what the Alerts
        screen calls "the last notification", and it has to keep meaning the
        last alert -- a test button able to overwrite the record of a blacklist
        message that failed would hide the one thing here that matters.
        """
        if kind == KIND_TEST:
            if outcome["ok"]:
                self._test_sent += 1
            else:
                self._test_failed += 1
            self._finish_test(outcome)
            return
        if outcome["ok"]:
            self._sent += 1
        else:
            self._failed += 1
        self._last = outcome

    def drain(self, timeout=20.0):
        """Wait for the queue to empty and the last delivery to finish.

        For the verification suite. Nothing in the app waits on the notifier.
        """
        limit = time.monotonic() + timeout
        while time.monotonic() < limit:
            if self._queue.unfinished_tasks == 0:
                return True
            time.sleep(0.05)
        return False
