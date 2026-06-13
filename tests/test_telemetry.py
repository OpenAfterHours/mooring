"""Telemetry sink dispatch, identity tagging, and fail-safe behavior."""

import json
import threading
import time

import pytest

from mooring import telemetry

IDENTITY = {
    "version": "9.9.9",
    "os_user": "tester",
    "host": "test-host",
    "os": "TestOS",
    "python": "3.12.0",
}


@pytest.fixture(autouse=True)
def _reset():
    telemetry._reset_for_tests()
    yield
    telemetry.flush(0.5)
    telemetry._reset_for_tests()


class FakeSession:
    """Stand-in for a requests.Session that records POSTs."""

    def __init__(self, raise_exc=None, block=None):
        self.posts = []
        self._raise = raise_exc
        self._block = block  # an Event the sender waits on, to simulate a hung POST

    def post(self, url, json=None, timeout=None):
        if self._block is not None:
            self._block.wait(5)
        if self._raise:
            raise self._raise
        self.posts.append((url, json, timeout))
        return type("R", (), {"status_code": 200})()


def _drain():
    telemetry.flush(2.0)


def test_disabled_when_no_destination(tmp_path):
    telemetry.configure("", identity=IDENTITY)
    telemetry.log_event("app_start", command="hub")
    _drain()
    assert telemetry._enabled is False
    assert list(tmp_path.iterdir()) == []  # nothing written anywhere


def test_url_sink_posts_json():
    sess = FakeSession()
    telemetry.configure("https://collector.example/mooring", identity=IDENTITY, session=sess)
    telemetry.log_event("push", pushed=3, conflicts=0, lines=4)
    _drain()
    assert len(sess.posts) == 1
    url, body, timeout = sess.posts[0]
    assert url == "https://collector.example/mooring"
    assert body["event"] == "push"
    assert body["pushed"] == 3
    assert body["version"] == "9.9.9"
    assert body["os_user"] == "tester"
    assert body["host"] == "test-host"
    assert body["ts"].endswith("Z")
    assert timeout is not None  # bounded so a hung endpoint can't stall the sender


def test_path_sink_appends_jsonl(tmp_path):
    folder = tmp_path / "logs"
    telemetry.configure(str(folder), identity=IDENTITY)
    telemetry.log_event("app_start", command="status")
    telemetry.log_event("pull", pulled=1)
    _drain()
    files = list(folder.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name == "tester@test-host.jsonl"
    lines = files[0].read_text("utf-8").strip().splitlines()
    assert len(lines) == 2  # second event appends, never truncates
    assert json.loads(lines[0])["command"] == "status"
    assert json.loads(lines[1])["event"] == "pull"


def test_per_user_filename_sanitized(tmp_path):
    ident = dict(IDENTITY, os_user="DOMAIN\\jdoe", host="host:42")
    telemetry.configure(str(tmp_path), identity=ident)
    telemetry.log_event("app_start", command="hub")
    _drain()
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1
    name = files[0].name
    assert "\\" not in name and ":" not in name and "/" not in name


@pytest.mark.parametrize(
    "dest,is_url",
    [
        ("http://x/y", True),
        ("https://x/y", True),
        ("HTTPS://X/Y", True),
        (r"\\srv\share\logs", False),
        ("D:/logs", False),
        ("/var/log/mooring", False),
    ],
)
def test_url_vs_path_autodetect(dest, is_url):
    sink = telemetry._resolve_sink(dest)
    assert isinstance(sink, telemetry._UrlSink) is is_url


def test_identity_on_every_event(tmp_path):
    telemetry.configure(str(tmp_path), identity=IDENTITY)
    telemetry.log_event("a")
    telemetry.log_event("b")
    _drain()
    lines = (tmp_path / "tester@test-host.jsonl").read_text("utf-8").strip().splitlines()
    for raw in lines:
        e = json.loads(raw)
        assert e["version"] == "9.9.9"
        assert e["os_user"] == "tester"
        assert e["ts"].endswith("Z")
        assert "event" in e


def test_set_user_tags_subsequent_events(tmp_path):
    telemetry.configure(str(tmp_path), identity=IDENTITY)
    telemetry.log_event("before")
    telemetry.set_user("octocat")
    telemetry.log_event("after")
    _drain()
    before, after = (
        json.loads(raw)
        for raw in (tmp_path / "tester@test-host.jsonl").read_text("utf-8").strip().splitlines()
    )
    assert before["user"] == ""
    assert after["user"] == "octocat"


def test_swallow_on_sink_error():
    sess = FakeSession(raise_exc=RuntimeError("network down"))
    telemetry.configure("https://x", identity=IDENTITY, session=sess)
    telemetry.log_event("push")  # must not raise
    _drain()  # must not raise
    assert sess.posts == []


def test_flush_timeout_is_bounded():
    release = threading.Event()
    sess = FakeSession(block=release)
    telemetry.configure("https://x", identity=IDENTITY, session=sess)
    telemetry.log_event("push")
    start = time.monotonic()
    telemetry.flush(0.2)
    assert time.monotonic() - start < 1.5  # a hung POST can't stall the exit
    release.set()  # let the sender finish so it doesn't block later tests


def test_level_error_drops_usage_events(tmp_path):
    telemetry.configure(str(tmp_path), identity=IDENTITY, level="error")
    telemetry.log_event("push", pushed=1)  # usage event, should be dropped
    telemetry.log_error(exc=ValueError("boom"))  # error, should pass
    _drain()
    lines = (tmp_path / "tester@test-host.jsonl").read_text("utf-8").strip().splitlines()
    events = [json.loads(raw)["event"] for raw in lines]
    assert events == ["error"]


def test_log_error_type_and_message_no_traceback(tmp_path):
    telemetry.configure(str(tmp_path), identity=IDENTITY)
    telemetry.log_error(exc=ValueError("boom"), op="push")
    _drain()
    event = json.loads((tmp_path / "tester@test-host.jsonl").read_text("utf-8").strip())
    assert event["event"] == "error"
    assert event["error_type"] == "ValueError"
    assert event["error_msg"] == "boom"
    assert event["op"] == "push"
    assert "traceback" not in event
    assert not any("Traceback" in str(v) for v in event.values())
