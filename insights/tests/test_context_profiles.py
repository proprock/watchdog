import pytest
from wdi.context_profiles import build_profile


def test_native_profile_has_no_overrides():
    profile = build_profile("claude", "native", 200000)
    assert profile.environment == {}
    assert profile.arguments == []


def test_claude_threshold_is_process_local():
    profile = build_profile("claude", "70", 200000)
    assert profile.environment["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "70"
    assert "--append-system-prompt" in profile.arguments


def test_codex_does_not_assume_setting_support():
    with pytest.raises(ValueError, match="verified"):
        build_profile("codex", "85", 200000)
    profile = build_profile(
        "codex", "85", 200000, codex_keys={"compact_prompt", "model_auto_compact_token_limit"}
    )
    assert "model_auto_compact_token_limit=170000" in profile.arguments


def test_launch_keeps_parent_environment_and_records_profile(monkeypatch, tmp_path):
    import json
    import os
    import sys
    from types import SimpleNamespace

    from wdi import context_profiles

    record = tmp_path / "experiment.json"
    monkeypatch.setenv("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE", "95")
    monkeypatch.setattr(
        sys,
        "argv",
        ["profiles", "claude", "--threshold", "70", "--launch", "--record", str(record)],
    )
    monkeypatch.setattr(context_profiles.shutil, "which", lambda _: "claude.exe")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs["env"]))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(context_profiles.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        context_profiles.main()
    assert exc.value.code == 0
    assert calls[0][1]["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "70"
    assert os.environ["CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"] == "95"
    assert json.loads(record.read_text())["version"] == "context-efficiency-v1"
