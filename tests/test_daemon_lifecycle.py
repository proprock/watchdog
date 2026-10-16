import json
from pathlib import Path

import pytest

from agent_watchdog import daemon_lifecycle


def test_missing_evidence_is_known_zero(tmp_path):
    assert daemon_lifecycle.startup_evidence(tmp_path, now=100) == {
        "starts_without_heartbeat": 0,
        "window_seconds": 60,
        "last_started_at": None,
    }


def test_starts_persist_across_helper_calls(tmp_path):
    daemon_lifecycle.record_start(tmp_path, now=100)
    daemon_lifecycle.record_start(tmp_path, now=120)
    assert daemon_lifecycle.startup_evidence(tmp_path, now=130) == {
        "starts_without_heartbeat": 2,
        "window_seconds": 60,
        "last_started_at": 120,
    }


def test_window_expires_without_status_writing_evidence(tmp_path):
    daemon_lifecycle.record_start(tmp_path, now=100)
    path = tmp_path / "daemon-health.json"
    before = path.read_bytes()
    assert daemon_lifecycle.startup_evidence(tmp_path, now=160)["starts_without_heartbeat"] == 1
    assert daemon_lifecycle.startup_evidence(tmp_path, now=161) == {
        "starts_without_heartbeat": 0,
        "window_seconds": 60,
        "last_started_at": 100,
    }
    assert path.read_bytes() == before


def test_new_start_prunes_expired_starts_and_caps_pending_history(tmp_path):
    daemon_lifecycle.record_start(tmp_path, now=1)
    for timestamp in range(100, 140):
        daemon_lifecycle.record_start(tmp_path, now=timestamp)
    assert daemon_lifecycle.startup_evidence(tmp_path, now=140)["starts_without_heartbeat"] == 32
    document = json.loads((tmp_path / "daemon-health.json").read_text())
    assert document["pending_starts"] == list(range(108, 140))
    assert (tmp_path / "daemon-health.json").stat().st_size < 8192


def test_first_heartbeat_resets_pending_starts_but_keeps_last_start(tmp_path):
    daemon_lifecycle.record_start(tmp_path, now=100)
    daemon_lifecycle.record_start(tmp_path, now=110)
    daemon_lifecycle.record_ready(tmp_path)
    assert daemon_lifecycle.startup_evidence(tmp_path, now=111) == {
        "starts_without_heartbeat": 0,
        "window_seconds": 60,
        "last_started_at": 110,
    }
    daemon_lifecycle.record_start(tmp_path, now=120)
    assert daemon_lifecycle.startup_evidence(tmp_path, now=121)["starts_without_heartbeat"] == 1


@pytest.mark.parametrize(
    "content",
    [
        b"not json",
        b"\xff",
        b"[]",
        b"{}",
        b'{"pending_starts": "bad", "last_started_at": null}',
        b'{"pending_starts": [NaN], "last_started_at": null}',
        b'{"pending_starts": [true], "last_started_at": null}',
        b'{"pending_starts": ["private path"], "last_started_at": null}',
        b'{"pending_starts": [], "last_started_at": Infinity}',
        b'{"pending_starts": [], "last_started_at": null}' + b" " * 8192,
    ],
)
def test_invalid_evidence_is_unavailable_and_start_does_not_raise(tmp_path, content):
    (tmp_path / "daemon-health.json").write_bytes(content)
    assert daemon_lifecycle.startup_evidence(tmp_path, now=100) == {"unavailable": True}
    daemon_lifecycle.record_start(tmp_path, now=100)


def test_unreadable_evidence_is_unavailable_and_mutations_do_not_raise(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("private path must not appear in evidence")

    monkeypatch.setattr(Path, "open", denied)
    assert daemon_lifecycle.startup_evidence(tmp_path, now=100) == {"unavailable": True}
    daemon_lifecycle.record_start(tmp_path, now=100)
    daemon_lifecycle.record_ready(tmp_path)


def test_failed_atomic_writes_do_not_raise_or_destroy_evidence(tmp_path, monkeypatch):
    daemon_lifecycle.record_start(tmp_path, now=100)

    def failed_write(*args, **kwargs):
        raise OSError("private path must not appear in evidence")

    monkeypatch.setattr(daemon_lifecycle, "atomic_write", failed_write)
    daemon_lifecycle.record_start(tmp_path, now=110)
    daemon_lifecycle.record_ready(tmp_path)
    assert daemon_lifecycle.startup_evidence(tmp_path, now=120)["starts_without_heartbeat"] == 1


def test_successful_heartbeat_replaces_unavailable_startup_history(tmp_path):
    (tmp_path / "daemon-health.json").write_bytes(b"invalid history")
    daemon_lifecycle.record_ready(tmp_path)
    assert daemon_lifecycle.startup_evidence(tmp_path, now=120) == {
        "starts_without_heartbeat": 0,
        "window_seconds": 60,
        "last_started_at": None,
    }


@pytest.mark.parametrize("now", [float("nan"), float("inf"), float("-inf"), True])
def test_nonfinite_or_boolean_start_time_is_rejected_without_raising(tmp_path, now):
    daemon_lifecycle.record_start(tmp_path, now=now)
    assert not (tmp_path / "daemon-health.json").exists()
