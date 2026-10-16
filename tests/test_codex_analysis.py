"""Provider-free contracts for the unavailable Codex analysis isolation gate."""

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from agent_watchdog.insights import llm

REQUEST = llm.Request("system", "private bundle", {"type": "object"}, "model", "low", 10)


@pytest.fixture
def preflight(monkeypatch):
    calls = []
    monkeypatch.setattr(llm.shutil, "which", lambda name: "/bin/codex")

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        assert Path(kwargs["cwd"]).is_dir()
        assert "private bundle" not in str(argv)
        assert "input" not in kwargs
        text = (
            "codex-cli 0.163.0-alpha.10.2\n"
            if argv[-1] == "--version"
            else "--ignore-user-config --ignore-rules --ephemeral --sandbox --output-schema --json"
        )
        return subprocess.CompletedProcess(argv, 0, text, "")

    monkeypatch.setattr(llm, "_run", run)
    return calls


def test_codex_refuses_to_send_evidence_without_a_proven_isolation_contract(preflight):
    result = llm.codex(replace(REQUEST, backend="codex"), forbidden_roots=[])
    assert result.reason == "isolation_unavailable"
    assert result.output is None
    assert [argv[1:] for argv, _ in preflight] == [["--version"], ["exec", "--help"]]
    assert all(not Path(kwargs["cwd"]).exists() for _, kwargs in preflight)
    assert all(0 < kwargs["timeout"] <= REQUEST.timeout for _, kwargs in preflight)
    assert result.provenance["provider"] == "codex"
    assert result.provenance["version"] == "0.163.0-alpha.10.2"
    assert result.provenance["requested_model"] == "model"
    assert result.provenance["requested_effort"] == "low"
    assert result.provenance["isolation"]["verified"] is False
    for key in ("usage", "cost_usd", "context_window", "max_output_tokens"):
        assert result.provenance[key] is None


def test_codex_missing_executable_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(llm.shutil, "which", lambda name: None)
    result = llm.runner("codex", forbidden_roots=[])(replace(REQUEST, backend="codex"))
    assert result.reason == "not_found"
    assert result.provenance["provider"] == "codex"


def test_codex_refuses_scratch_inside_registered_project(preflight):
    result = llm.codex(REQUEST, forbidden_roots=[Path(llm.tempfile.gettempdir())])
    assert result.reason == "isolation_unavailable"
    assert preflight == []


@pytest.mark.parametrize("failure", ["nonzero", "malformed", "timeout", "missing"])
def test_codex_preflight_failures_never_invoke_analysis(monkeypatch, preflight, failure):
    def run(argv, **kwargs):
        preflight.append((argv, kwargs))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        if failure == "missing":
            raise FileNotFoundError
        return subprocess.CompletedProcess(argv, int(failure == "nonzero"), "invalid", "")

    monkeypatch.setattr(llm, "_run", run)
    result = llm.codex(REQUEST, forbidden_roots=[])
    expected = {"timeout": "timeout", "missing": "not_found"}.get(failure, "isolation_unavailable")
    assert result.reason == expected
    assert len(preflight) == 1
    assert not Path(preflight[0][1]["cwd"]).exists()


def test_codex_cancellation_removes_scratch_and_propagates(monkeypatch, preflight):
    def cancel(argv, **kwargs):
        preflight.append((argv, kwargs))
        raise KeyboardInterrupt

    monkeypatch.setattr(llm, "_run", cancel)
    with pytest.raises(KeyboardInterrupt):
        llm.codex(REQUEST, forbidden_roots=[])
    assert not Path(preflight[0][1]["cwd"]).exists()


def test_backend_factory_rejects_unknown_backend():
    with pytest.raises(ValueError, match="backend"):
        llm.runner("unknown", forbidden_roots=[])


def test_backend_factory_rejects_request_backend_mismatch(preflight):
    result = llm.runner("codex", forbidden_roots=[])(REQUEST)
    assert result.reason == "backend_mismatch"
    assert preflight == []


def test_new_version_or_promising_help_text_cannot_automatically_activate_backend(
    monkeypatch, preflight
):
    def run(argv, **kwargs):
        preflight.append((argv, kwargs))
        stdout = "codex-cli 99.0.0" if argv[-1] == "--version" else "--safe-mode --tools"
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(llm, "_run", run)
    result = llm.codex(REQUEST, forbidden_roots=[])
    assert result.reason == "isolation_unavailable"
    assert result.provenance["version"] == "99.0.0"
    assert len(preflight) == 2


def test_expired_preflight_budget_starts_no_process(preflight):
    result = llm.codex(replace(REQUEST, timeout=0), forbidden_roots=[])
    assert result.reason == "timeout"
    assert preflight == []
