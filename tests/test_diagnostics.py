from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agent_watchdog.config import Config, Limits, Overrides
from agent_watchdog.diagnostics import emit, error_code


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


def test_acknowledged_and_unavailable_decisions_reach_the_log(tmp_path):
    limits = Limits(log_level="DEBUG")
    emit(tmp_path, limits, "INFO", component="daemon", event="control", decision="acknowledged")
    emit(
        tmp_path,
        limits,
        "WARNING",
        component="daemon",
        event="enrichment",
        decision="unavailable",
        error_type="transcript_sources_unavailable",
    )
    body = (tmp_path / "watchdog.log").read_text(encoding="ascii")
    assert "event=control decision=acknowledged" in body
    assert "event=enrichment decision=unavailable error_type=transcript_sources_unavailable" in body


def test_error_type_accepts_internal_codes_but_rejects_free_text(tmp_path):
    limits = Limits(log_level="DEBUG")
    emit(
        tmp_path,
        limits,
        "WARNING",
        component="daemon",
        event="spool",
        decision="discarded",
        error_type="usage_counter_reset",
    )
    for rejected in ("Traceback (most recent call last)", "C:/secret/path", "x" * 64):
        emit(
            tmp_path,
            limits,
            "WARNING",
            component="daemon",
            event="spool",
            decision="discarded",
            error_type=rejected,
        )
    lines = (tmp_path / "watchdog.log").read_text(encoding="ascii").splitlines()
    assert len(lines) == 1
    assert lines[0].endswith("error_type=usage_counter_reset")


def test_log_detail_is_off_by_default_and_stays_content_free(tmp_path):
    emit(
        tmp_path,
        Limits(log_level="DEBUG"),
        "WARNING",
        component="daemon",
        event="spool",
        decision="discarded",
        reason="invalid",
        detail="C:/Users/alice/repo/secret.py: token=sk-proj-" + "a" * 40,
    )
    body = (tmp_path / "watchdog.log").read_text(encoding="ascii")
    assert "detail=" not in body


def test_log_detail_appends_a_sanitized_bounded_single_line_clause(tmp_path):
    secret = "sk-proj-" + "a" * 40
    raw = f'1 validation error\nBearer {secret}\npath C:\\Users\\alice\\x "q" ' + "z" * 400
    emit(
        tmp_path,
        Limits(log_level="DEBUG", log_detail=True),
        "WARNING",
        component="daemon",
        event="spool",
        decision="discarded",
        reason="invalid",
        error_type="valueerror",
        detail=raw,
    )
    lines = (tmp_path / "watchdog.log").read_text(encoding="ascii").splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert secret not in line and "Bearer" not in line
    assert "C:\\Users\\alice\\x" in line  # paths are retained on purpose
    assert '""' not in line
    detail = line.split(' detail="', 1)[1][:-1]
    assert len(detail) <= 203 and detail.endswith("...")
    assert line.index("error_type=valueerror") < line.index('detail="')


def test_error_code_preserves_the_exception_class_name(tmp_path):
    class TranscriptUnreadable(Exception):
        pass

    assert error_code(TranscriptUnreadable()) == "transcriptunreadable"
    assert error_code(ValueError("boom")) == "valueerror"

    weird = type("Weird Name!", (Exception,), {})
    assert error_code(weird()) == "unexpected"


def test_log_level_is_strict_and_log_limits_are_not_project_overrides():
    with pytest.raises(ValidationError):
        Limits.model_validate({"log_level": "debug"})
    with pytest.raises(ValidationError):
        Overrides.model_validate({"log_files": 2})
    assert Config().defaults.log_level == "INFO"
