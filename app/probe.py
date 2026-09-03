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
import multiprocessing as mp
import queue as queue_mod
import time

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
    return cv2.VideoCapture(
        target,
        cv2.CAP_ANY,
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


def test_source(uri, timeout=TEST_TIMEOUT_SEC):
    """Open a source once and report what came back, with a preview frame."""
    return _run(
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
