"""Fire-and-forget usage/error telemetry to an admin-configured central sink.

The admin who builds the .pyz bakes a destination into ``[logging] endpoint``:
an ``http(s)://`` URL receives each event as a JSON POST; any other value is
treated as a folder/UNC path and gets a per-user JSONL file appended to it
(``<os_user>@<host>.jsonl``). An empty endpoint disables telemetry entirely —
that is the shipped default, and what keeps the test suite hermetic.

Everything here is best-effort and must never block or crash the app (the same
spirit as the truststore guard in ``cli.py``): ``log_event`` only enqueues, a
daemon thread drains the queue, and all sink I/O is wrapped so a slow or
unreachable destination can at worst drop events. An ``atexit`` flush with a
wall-clock-bounded wait makes sure a short-lived CLI run still ships its events
without an unreachable endpoint hanging the exit.
"""

from __future__ import annotations

import atexit
import getpass
import json
import platform
import queue
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from mooring import __version__

# Severity gate: "info" lets everything through, "error" keeps only errors.
_INFO = 20
_ERROR = 40
_LEVELS = {"info": _INFO, "error": _ERROR}

_lock = threading.Lock()
_queue: queue.Queue = queue.Queue()
_thread: threading.Thread | None = None
_sink = None  # callable(event: dict) -> None
_identity: dict = {}
_user_login = ""
_enabled = False
_level = _INFO
_session = None  # lazy/injected requests.Session for URL sinks
_atexit_registered = False


# -- sinks -------------------------------------------------------------------


class _UrlSink:
    def __init__(self, url: str) -> None:
        self.url = url

    def __call__(self, event: dict) -> None:
        # truststore.inject_into_ssl() has already run in cli.main(), so this
        # POST verifies against the corporate trust store like the rest of the app.
        _get_session().post(self.url, json=event, timeout=(3.05, 3))


class _PathSink:
    def __init__(self, folder: str) -> None:
        self.folder = Path(folder)

    def __call__(self, event: dict) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        name = _safe_filename(event.get("os_user", "user"), event.get("host", "host"))
        # One open-append-close per event: a share blip loses at most one line,
        # and the per-user filename means concurrent users never clobber each other.
        with (self.folder / name).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")


def _safe_filename(os_user: str, host: str) -> str:
    def clean(value: str) -> str:
        return "".join(c for c in str(value) if c.isalnum() or c in "-_.") or "unknown"

    return f"{clean(os_user)}@{clean(host)}.jsonl"


def _resolve_sink(destination: str):
    dest = (destination or "").strip()
    if not dest:
        return None
    if dest.lower().startswith(("http://", "https://")):
        return _UrlSink(dest)
    return _PathSink(dest)


def _get_session():
    global _session
    if _session is None:
        import requests

        _session = requests.Session()
    return _session


# -- public API --------------------------------------------------------------


def base_identity() -> dict:
    """The machine/app fields stamped onto every event."""
    return {
        "version": __version__,
        "os_user": _safe(getpass.getuser),
        "host": _safe(socket.gethostname),
        "os": _safe(platform.platform),
        "python": platform.python_version(),
    }


def configure(destination: str, *, identity: dict, level: str = "info", session=None) -> None:
    """Point telemetry at a destination. Blank destination = disabled (no-op).

    Idempotent and total: any failure here just leaves telemetry disabled — it
    must never raise into the caller.
    """
    global _sink, _identity, _enabled, _level, _thread, _session, _atexit_registered
    try:
        with _lock:
            _identity = dict(identity or {})
            _level = _LEVELS.get(str(level).strip().lower(), _INFO)
            _session = session
            sink = _resolve_sink(destination)
            if sink is None:
                _enabled = False
                _sink = None
                return
            _sink = sink
            _enabled = True
            _ensure_thread()
            if not _atexit_registered:
                atexit.register(flush)
                _atexit_registered = True
    except Exception:  # noqa: BLE001  # telemetry must never break the app
        _enabled = False
        _sink = None


def _ensure_thread() -> None:
    """Start the single background sender once and keep it for the process.

    A singleton matters: configure() runs once in production, but the test suite
    reconfigures repeatedly, and two live daemons draining one queue would
    reorder events. The daemon reads the module-level _sink each loop, so
    swapping sinks needs no new thread.
    """
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_run, name="mooring-telemetry", daemon=True)
        _thread.start()


def set_user(login: str) -> None:
    """Record the GitHub login once it's known; overlaid onto later events."""
    global _user_login
    if login:
        _user_login = str(login)


def log_event(name: str, **fields) -> None:
    _emit(name, _INFO, fields)


def log_error(*, exc: BaseException, **fields) -> None:
    """Record an error as type + message only — never a traceback."""
    fields = dict(fields)
    fields["error_type"] = type(exc).__name__
    fields["error_msg"] = str(exc)
    _emit("error", _ERROR, fields)


def flush(timeout: float = 3.0) -> None:
    """Wait (at most ``timeout`` seconds) for queued events to be sent."""
    try:
        if not _enabled:
            return
        deadline = time.monotonic() + timeout
        while _queue.unfinished_tasks > 0 and time.monotonic() < deadline:
            time.sleep(0.02)
    except Exception:  # noqa: BLE001  # flushing is best-effort
        pass


# -- internals ---------------------------------------------------------------


def _emit(name: str, severity: int, fields: dict) -> None:
    if not _enabled or severity < _level:
        return
    try:
        event = dict(_identity)
        event["ts"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        event["event"] = name
        event["user"] = _user_login
        event.update(fields)
        _queue.put_nowait(event)
    except Exception:  # noqa: BLE001  # dropping an event must never raise
        pass


def _run() -> None:
    while True:
        item = _queue.get()
        try:
            sink = _sink
            if item is not None and sink is not None:
                sink(item)
        except Exception:  # noqa: BLE001  # drop the event, never die
            pass
        finally:
            _queue.task_done()


def _safe(fn, default: str = "unknown") -> str:
    try:
        return str(fn())
    except Exception:  # noqa: BLE001
        return default


def _reset_for_tests() -> None:
    """Drop sink/state and drain the queue so tests don't leak into each other.

    The singleton daemon is deliberately left running (it reads _sink=None now,
    so it's a no-op until the next configure()).
    """
    global _sink, _identity, _user_login, _enabled, _level, _session
    with _lock:
        _enabled = False
        _sink = None
        _identity = {}
        _user_login = ""
        _level = _INFO
        _session = None
        try:
            while True:
                _queue.get_nowait()
                _queue.task_done()
        except queue.Empty:
            pass
