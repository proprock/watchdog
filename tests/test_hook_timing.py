import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures/hooks/claude_transcript.jsonl"


@pytest.fixture
def analyzer():
    path = Path(__file__).parents[1] / "scripts" / "hook_timing.py"
    spec = importlib.util.spec_from_file_location("hook_timing", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def transcript_dir(tmp_path):
    nested = tmp_path / "projects" / "slug"
    nested.mkdir(parents=True)
    shutil.copy(FIXTURE, nested / "session.jsonl")
    return tmp_path


def test_report_aggregates_durations_ids_and_events(analyzer, transcript_dir):
    report = analyzer.analyze(transcript_dir, "WD-TOKEN")
    assert report["schema_version"] == 1 and report["token"] == "WD-TOKEN"
    assert report["matched_hook_entries"] == 3
    assert report["duration_ms"] == {"count": 2, "p50_ms": 120.0, "p95_ms": 240.0, "max_ms": 240.0}
    assert report["missing_duration"] == 1
    assert report["per_event"] == {"PostToolUse": 1, "PreToolUse": 2}
    assert report["tool_use_ids"] == ["toolu_a"] and report["tool_use_id_count"] == 1
    assert report["lines_skipped"] == 2


def test_report_never_leaks_command_or_output_text(analyzer, transcript_dir):
    blob = json.dumps(analyzer.analyze(transcript_dir, "WD-TOKEN"))
    assert "/secret/path" not in blob
    assert "synthetic output text" not in blob
    assert "other-plugin-hook" not in blob


def test_unknown_token_matches_nothing(analyzer, transcript_dir):
    report = analyzer.analyze(transcript_dir, "no-such-token")
    assert report["matched_hook_entries"] == 0
    assert report["duration_ms"]["count"] == 0 and report["duration_ms"]["p95_ms"] is None
    # Tool-use ids are still reported; they do not depend on the token.
    assert report["tool_use_ids"] == ["toolu_a"]


def test_byte_cap_truncates_without_failing(analyzer, transcript_dir):
    report = analyzer.analyze(transcript_dir, "WD-TOKEN", max_bytes=200)
    assert report["lines_read"] < 7
    assert report["schema_version"] == 1


def test_cli_prints_json_and_writes_output(analyzer, transcript_dir, tmp_path, monkeypatch, capsys):
    out = tmp_path / "evidence" / "timing.json"
    monkeypatch.setattr(
        sys,
        "argv",
        ["hook_timing.py", str(transcript_dir), "--token", "WD-TOKEN", "--output", str(out)],
    )
    analyzer.main()
    printed = json.loads(capsys.readouterr().out)
    assert printed == json.loads(out.read_text())
    assert printed["matched_hook_entries"] == 3
