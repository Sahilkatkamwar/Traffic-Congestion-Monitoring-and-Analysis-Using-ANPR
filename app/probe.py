"""Connection testing and webcam detection, each in its own process.

Both of these open a capture, and opening a capture is the one thing in this
app that can block indefinitely. A phone that has left the network, a DirectShow
driver mid-crash, an RTSP host that accepts the TCP connection and then says
nothing -- OpenCV's timeouts help but do not cover every case, and an API worker
thread stuck in `cv2.VideoCapture()` is a thread that never comes back.

So neither runs in the API process. Each is a spawned child that opens one
capture and exits, and the parent waits with a deadline it controls. A child
that hangs is terminated and reported as a timeout; nothing in the server is
held.

Spawn rules apply: the targets below are module-level, every argument is
picklable, and the child builds its own capture. Nothing open crosses.
"""

import base64
import errno
import ipaddress
import multiprocessing as mp
import queue as queue_mod
import socket
import threading
import time
import urllib.parse

# How long the parent waits before giving up on a child and killing it. The
# capture's own open timeout is 8s, so this has to be longer or every slow-but-
# working camera is reported as a hang.
TEST_TIMEOUT_SEC = 20.0
DEVICE_TIMEOUT_SEC = 30.0

# Indices tried when looking for webcams. DirectShow has no enumeration API we
# can reach from OpenCV, so the only way to find a camera is to open it.
MAX_DEVICE_INDEX = 4

PREVIEW_MAX_WIDTH = 640
# Frames read before the preview is taken. The first frame off a webcam is
# routinely black or half-exposed while the sensor settles, and a black preview
# looks exactly like a broken camera.
WARMUP_FRAMES = 4
# Frames timed to measure the real rate of a live source. OpenCV reports 0 or a
# nominal value for one, and the measured rate is what the worker uses too.
FPS_FRAMES = 12


def _encode(frame):
    """A frame as a data URI, scaled down. Returns None if it will not encode."""
    import cv2

    height, width = frame.shape[:2]
    if width > PREVIEW_MAX_WIDTH:
        scale = PREVIEW_MAX_WIDTH / width
        frame = cv2.resize(
            frame, (PREVIEW_MAX_WIDTH, max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
    if not ok:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


def _open(target):
    """Open a capture the same way the worker does, or return None.

    Imported here rather than at module scope so this module stays importable
    without cv2 in the parent's import graph doing anything at collection time.
    """
    import cv2

    from app.worker import OPEN_TIMEOUT_MS, READ_TIMEOUT_MS

    if isinstance(target, int):
        return cv2.VideoCapture(target, cv2.CAP_DSHOW)
    # Same backend rule as app/worker.py:_open_capture, and for the same
    # reason: through CAP_ANY the timeouts below are not honoured, so a dead
    # address costs the connection test its whole deadline instead of 8s and
    # is reported as a hang rather than as a refusal.
    backend = cv2.CAP_FFMPEG if "://" in str(target) else cv2.CAP_ANY
    return cv2.VideoCapture(
        target,
        backend,
        [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, OPEN_TIMEOUT_MS,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC, READ_TIMEOUT_MS,
        ],
    )


def _inspect(target, want_preview=True):
    """Open, read a frame, and report what the source actually is."""
    import cv2

    from app.sources import resolve_uri  # noqa: F401  (kept for import symmetry)

    cap = None
    try:
        cap = _open(target)
        if cap is None or not cap.isOpened():
            return {
                "ok": False,
                "error": (
                    f"Could not open {target}. Check the file exists, the camera "
                    f"is not already in use by another app, or that the URL "
                    f"responds in a browser."
                ),
            }

        frame = None
        for _ in range(WARMUP_FRAMES):
            ok, candidate = cap.read()
            if ok and candidate is not None:
                frame = candidate
        if frame is None:
            ok, frame = cap.read()
            if not ok or frame is None:
                return {
                    "ok": False,
                    "error": (
                        f"{target} opened but sent no video. If this is a phone, "
                        f"check the camera app is still on the streaming screen."
                    ),
                }

        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        reported = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if not 0 < reported < 240:
            reported = 0.0
        recorded = total >= 1

        measured = None
        if not recorded:
            # A live source has no reliable fps from OpenCV, so time it. This is
            # the same measurement the worker makes over a rolling window.
            started = time.monotonic()
            read = 0
            for _ in range(FPS_FRAMES):
                ok, _ = cap.read()
                if not ok:
                    break
                read += 1
            span = time.monotonic() - started
            if read > 1 and span > 0:
                measured = read / span

        height, width = frame.shape[:2]
        return {
            "ok": True,
            "recorded": recorded,
            "frames": total if recorded else None,
            "fps": reported or measured,
            "fps_measured": measured is not None,
            "width": int(width),
            "height": int(height),
            "duration_sec": (total / reported) if recorded and reported else None,
            "preview": _encode(frame) if want_preview else None,
        }
    except Exception as exc:  # noqa: BLE001 - the reason has to reach the UI
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if cap is not None:
            # Windows keeps a lock on a video file held by a dead handle, and a
            # webcam left open here cannot be opened by the worker afterwards.
            cap.release()


def _test_child(uri, out):
    """Spawn target: probe one source and put the verdict on the queue."""
    from app.sources import resolve_uri

    try:
        result = _inspect(resolve_uri(uri))
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    result["uri"] = str(uri)
    out.put(result)


def _devices_child(indices, out):
    """Spawn target: try each webcam index and report the ones that answer."""
    found = []
    for index in indices:
        result = _inspect(index, want_preview=False)
        if result.get("ok"):
            found.append(
                {
                    "index": index,
                    "uri": str(index),
                    "width": result.get("width"),
                    "height": result.get("height"),
                    "fps": result.get("fps"),
                }
            )
    out.put(found)


def _run(target, args, timeout, fallback):
    """Run one spawn target with a deadline. A child that overruns is killed."""
    ctx = mp.get_context("spawn")
    out = ctx.Queue(maxsize=1)
    process = ctx.Process(target=target, args=args + (out,), daemon=True)
    process.start()
    try:
        return out.get(timeout=timeout)
    except queue_mod.Empty:
        return fallback
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)
        out.close()
        out.join_thread()


# Addresses that cannot be another device on this network, whatever the phone
# is doing. Only these, and nothing that merely looks distant: a camera on a
# routed subnet is legitimate, and this machine's own netmask is not readable
# without a dependency, so guessing "off-subnet" would produce a confident
# wrong answer. Each of these is wrong by definition instead.
UNREACHABLE_PREFIXES = (
    (
        ipaddress.ip_network("192.0.0.0/24"),
        "192.0.0.x is the prefix Android uses for its own 464XLAT translation "
        "(RFC 7335). IP Webcam lists it beside the real ones and it means "
        "nothing on any other machine.",
    ),
    (
        ipaddress.ip_network("169.254.0.0/16"),
        "169.254.x is a link-local address, which a device gives itself when "
        "nothing handed it one.",
    ),
    (
        ipaddress.ip_network("127.0.0.0/8"),
        "127.x is this machine talking to itself, not the phone.",
    ),
)


def _host_of(uri):
    """The IPv4 address in a uri, or None if it does not carry one."""
    text = str(uri).strip()
    if not text:
        return None
    if "://" not in text:
        text = "//" + text
    try:
        host = urllib.parse.urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None  # a name, which says nothing about where it is


def _own_addresses():
    """This machine's own routable IPv4 addresses, for the person to compare."""
    found = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        return found
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local:
            continue
        if address not in found:
            found.append(address)
    return found


def address_hint(uri):
    """Why this address can never answer, when the address itself says so.

    A phone camera's URL is typed in by hand from what the app on the phone
    shows, and IP Webcam shows every address the phone holds. Some of them are
    the phone's own internal ones and are unreachable from anywhere else --
    they still ping, because something upstream answers, which is what makes
    them so convincing. Saying which is more use than "did not respond".
    """
    host = _host_of(uri)
    if host is None or host.version != 4:
        return None
    for network, why in UNREACHABLE_PREFIXES:
        if host in network:
            mine = _own_addresses()
            where = (
                f" This machine is on {', '.join(mine)}, so read the address off "
                f"the phone's own screen and pick the one that starts the same way."
                if mine
                else ""
            )
            return f"{why}{where}"
    return None


# Ports a source URL implies when it does not carry one. Only used to ask the
# network a question; nothing here selects a backend or changes what is opened.
_SCHEME_PORTS = {"http": 80, "https": 443, "rtsp": 554, "rtsps": 322}

# A device that is there and says no. WSAECONNREFUSED is 10061 and
# WSAECONNRESET 10054; the errno names are carried too because they are
# what a non-Windows build reports.
_REFUSED = {
    10061, 10054,
    getattr(errno, "ECONNREFUSED", None),
    getattr(errno, "ECONNRESET", None),
} - {None}

# Long enough for a phone on a busy Wi-Fi to answer, short enough that asking
# costs less than the open that already failed. Measured on this machine, a
# refusal on loopback takes 2.0s to come back -- security software sits in the
# path -- so a 2s deadline would file the clearest answer available as silence.
REACH_TIMEOUT_SEC = 4.0


def reachability(uri, timeout=REACH_TIMEOUT_SEC):
    """What the network says about an address whose capture would not open.

    The three failures a person has to tell apart look identical in OpenCV's
    output -- every one of them is `Connection to tcp://host:port failed`. They
    are not the same problem and they do not have the same fix, so the question
    is put to a plain socket, which can distinguish them:

    - the connection is accepted: the address is fine and the fault is in what
      it served, so nothing is added here;
    - the connection is refused: the device is there and no camera app is
      listening on that port;
    - nothing comes back at all: the device is not on this network, or the
      network does not let its clients reach each other.

    Returns a sentence to append to the failure, or None.
    """
    text = str(uri).strip()
    if "://" not in text:
        return None  # a file or a webcam index; the network is not involved
    try:
        parts = urllib.parse.urlsplit(text)
        host = parts.hostname
        port = parts.port or _SCHEME_PORTS.get((parts.scheme or "").lower())
    except ValueError:
        return None
    if not host or not port:
        return None

    # Three connect styles were measured on this machine and only one of them
    # answers the question. A blocking socket is the one that does, and it is
    # put on a deadline because it is also the one with no timeout of its own:
    #
    # - `connect_ex` on a socket carrying a timeout is a non-blocking connect,
    #   so it returns WSAEWOULDBLOCK (10035) at once for refused, dropped and
    #   accepted alike -- it distinguishes nothing;
    # - a non-blocking connect with `select` on both the writable and the
    #   exception set, which is the documented way to do this, reports a closed
    #   port on 127.0.0.1 as neither for the full timeout, with SO_ERROR 0. So
    #   does `create_connection`, which waits only for writability and raises
    #   TimeoutError. Both report the loudest failure there is as silence;
    # - a blocking connect to the same closed port raises ConnectionRefusedError
    #   (10061), correctly, in 2.0s.
    #
    # The thread is how the blocking connect is bounded: left alone it would
    # take the OS default on a silent address, which is around 21s on Windows
    # and far past the 8s an open is allowed. A daemon thread outliving the
    # answer costs nothing -- it holds one socket and exits when the OS gives
    # up -- and this only ever runs after an open has already failed.
    answer = {}

    def _attempt():
        sock = socket.socket()
        try:
            sock.connect((host, port))
            answer["err"] = 0
        except OSError as exc:
            answer["err"] = exc.errno
        finally:
            sock.close()

    thread = threading.Thread(target=_attempt, daemon=True)
    thread.start()
    thread.join(timeout)
    err = answer.get("err")  # None while it is still trying

    if err == 0:
        return None  # accepted; whatever is wrong is in what it served
    if err in _REFUSED:
        return (
            f"{host} is on the network but nothing is listening on port {port}. "
            f"The device is reachable, so this is the camera app rather than the "
            f"connection: check it is running and still on its streaming screen, "
            f"and that the port it shows is {port}."
        )
    return (
        f"Nothing at {host} answered on port {port}. Check the phone is awake "
        f"and joined to this Wi-Fi, and that its address has not changed -- most "
        f"networks hand out a new one each time a device reconnects. If the "
        f"address is right and it still does not answer, the network may be "
        f"keeping its devices apart; guest and campus Wi-Fi usually do."
    )


def network_hint(uri):
    """The one sentence worth adding to a failed open, or None.

    The address being wrong by definition is a stronger statement than anything
    a probe can measure, so it wins; asking the network is what is left.
    """
    return address_hint(uri) or reachability(uri)


def test_source(uri, timeout=TEST_TIMEOUT_SEC):
    """Open a source once and report what came back, with a preview frame."""
    result = _run(
        _test_child,
        (uri,),
        timeout,
        {
            "ok": False,
            "uri": str(uri),
            "error": (
                f"{uri} did not respond within {int(timeout)}s. Check the device "
                f"is powered on and on this network, then try again."
            ),
        },
    )
    # Appended to a failure, never raised on its own: an address being odd is
    # not itself a reason to refuse a test, and the test is what knows whether
    # anything answered.
    hint = network_hint(uri)
    if hint and isinstance(result, dict) and not result.get("ok") and result.get("error"):
        result["error"] = f"{result['error']} {hint}"
    return result


def detect_webcams(skip=(), max_index=MAX_DEVICE_INDEX, timeout=DEVICE_TIMEOUT_SEC):
    """Webcam indices that answer.

    `skip` is the set of indices a running worker already holds. Probing one
    would either fail -- reporting a working camera as absent -- or, worse,
    succeed and take the device away from the worker mid-run.
    """
    skipped = {int(s) for s in skip if str(s).isdigit()}
    indices = [i for i in range(max_index + 1) if i not in skipped]
    if not indices:
        return []
    found = _run(_devices_child, (indices,), timeout, [])
    return found if isinstance(found, list) else []
