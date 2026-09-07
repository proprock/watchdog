from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_watchdog.config import Config, Limits, Overrides
from agent_watchdog.diagnostics import emit


def records(data):
    return [
        path.read_text(encoding="ascii")
        for path in sorted(data.glob("watchdog.log*"))
        if path.name != "watchdog.log.lock"
    ]


def test_default_level_hides_debug_and_writes_safe_human_readable_info(tmp_path):
    limits = Limits()
    project_id, event_id = uuid4(), uuid4()
    emit(tmp_path, limits, "DEBUG", component="daemon", event="poll", decision="idle")
    emit(
        tmp_path,
        limits,
        "INFO",
        component="daemon",
        event="spool",
        decision="admitted",
        project_id=project_id,
        event_id=event_id,
        count=1,
    )
    body = (tmp_path / "watchdog.log").read_text(encoding="ascii")
    assert "level=INFO component=daemon event=spool decision=admitted" in body
    assert str(project_id) in body and str(event_id) in body
    assert "poll" not in body


def test_debug_level_and_rotation_keep_active_plus_configured_archives(tmp_path):
    limits = Limits(log_level="DEBUG", log_files=3, log_bytes=120)
    for index in range(8):
        emit(tmp_path, limits, "DEBUG", component="daemon", event="poll", count=index)
    assert [
        path.name for path in sorted(tmp_path.glob("watchdog.log*")) if path.suffix != ".lock"
    ] == [
        "watchdog.log",
        "watchdog.log.1",
        "watchdog.log.2",
    ]
    assert all(len(body) <= limits.log_bytes for body in records(tmp_path))


def test_log_writer_is_content_free_and_best_effort_for_invalid_fields(tmp_path):
    secret = "sk-proj-" + "a" * 40
    emit(tmp_path, Limits(log_level="DEBUG"), "DEBUG", component="daemon", event="poll")
    emit(tmp_path, Limits(log_level="DEBUG"), "DEBUG", component="daemon", event=secret)
    body = "".join(records(tmp_path))
    assert secret not in body
    assert "prompt" not in body and "tool_output" not in body


def test_concurrent_writers_leave_complete_lines(tmp_path):
    limits = Limits(log_level="DEBUG", log_bytes=1024**2)

    def write(index):
        emit(tmp_path, limits, "DEBUG", component="daemon", event="poll", count=index)

    with ThreadPoolExecutor(max_workers=8) as workers:
        list(workers.map(write, range(40)))
    lines = (tmp_path / "watchdog.log").read_text(encoding="ascii").splitlines()
    assert 1 <= len(lines) <= 40
    assert all(line.startswith("timestamp=") and " event=poll count=" in line for line in lines)


def test_log_level_is_strict_and_log_limits_are_not_project_overrides():
    with pytest.raises(ValidationError):
        Limits.model_validate({"log_level": "debug"})
    with pytest.raises(ValidationError):
        Overrides.model_validate({"log_files": 2})
    assert Config().defaults.log_level == "INFO"
