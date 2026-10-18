"""Approved-byte Python execution and cooperative supervision (WD-143)."""

import hashlib
import threading
import time
from dataclasses import replace
from typing import cast

import pytest

from agent_watchdog.config import Config, Rules, UserPaths
from agent_watchdog.rules.api import Context
from agent_watchdog.rules.python import PythonHost, PythonRule, PythonRuleTimeout, parse_python
from agent_watchdog.rules.registry import RuleRegistry


def source(body="return Decision(action='deny', reason='blocked')", *, init="", timeout=1000):
    metadata = dict(
        name="custom",
        version="1",
        origin="user",
        provider=["claude"],
        event="PreToolUse",
        action=dict(kind="deny", message="blocked"),
        timeout_ms=timeout,
    )
    return (
        f"RULE = {metadata!r}\nfrom agent_watchdog.rules.api import Decision\n"
        f"{init}\ndef evaluate(ctx):\n    {body}\n"
    ).encode()


@pytest.fixture
def ctx(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "run")
    return Context(
        paths, Config(), "claude", {"hook_event_name": "PreToolUse", "tool_input": {"x": [1]}}
    )


def run(host, ctx, data, *, digest=None, on_error=None):
    return host.evaluate(
        parse_python(data), data, digest or hashlib.sha256(data).hexdigest(), ctx, on_error=on_error
    )


def test_unapproved_bytes_never_initialize_and_snapshot_does_not_reopen_path(ctx, tmp_path):
    marker = tmp_path / "executed"
    data = source(init=f"from pathlib import Path\nPath({str(marker)!r}).touch()")
    host = PythonHost()
    rule = parse_python(data)
    assert host.evaluate(rule, data, None, ctx).action == "allow"
    assert host.evaluate(rule, data, "0" * 64, ctx).action == "allow"
    assert not marker.exists()
    assert run(host, ctx, data).action == "deny"
    assert marker.exists()


@pytest.mark.parametrize("stage", ["init", "evaluate"])
def test_timeout_ignores_late_result_disables_until_restart_and_retains_slot(
    ctx, monkeypatch, stage
):
    gate = threading.Event()
    entered = threading.Event()
    monkeypatch.setattr(threading, "_wd143_gate", gate, raising=False)
    monkeypatch.setattr(threading, "_wd143_entered", entered, raising=False)
    wait = "import threading; threading._wd143_entered.set(); threading._wd143_gate.wait()"
    data = source(
        init=wait if stage == "init" else "",
        body=(wait + "; return Decision(action='deny')")
        if stage == "evaluate"
        else "return Decision(action='deny')",
        timeout=150,
    )
    host = PythonHost(max_workers=1)
    errors = []
    try:
        start = time.monotonic()
        assert run(host, ctx, data, on_error=lambda n, e: errors.append((n, e))).action == "allow"
        assert time.monotonic() - start < 2
        assert entered.is_set()
        assert host.status("custom") == "disabled"
        assert len(errors) == 1 and isinstance(errors[0][1], TimeoutError)
        other = source().replace(b"'custom'", b"'other'")
        assert run(host, ctx, other).action == "allow"
        gate.set()
        for _ in range(100):
            if run(host, ctx, other).action == "deny":
                break
            time.sleep(0.01)
        else:
            pytest.fail("worker slot was not released after thread exit")
        assert run(host, ctx, data).action == "allow"
        assert run(host, ctx, source()).action == "allow"
        assert run(PythonHost(), ctx, source()).action == "deny"
    finally:
        gate.set()


@pytest.mark.parametrize(
    "body,init",
    [
        ("raise ValueError('bad')", ""),
        ("return Decision()", "raise RuntimeError('init')"),
        ("return {}", ""),
        ("raise SystemExit(2)", ""),
    ],
)
def test_exceptions_and_invalid_results_degrade_and_fail_open(ctx, body, init):
    errors = []
    host = PythonHost()
    assert (
        run(host, ctx, source(body, init=init), on_error=lambda n, e: errors.append(e)).action
        == "allow"
    )
    assert host.status("custom") == "degraded"
    assert errors


def test_context_is_deeply_readonly_and_does_not_mutate_daemon_input(ctx):
    data = source("ctx.hook_input['tool_input']['x'].append(2); return Decision(action='deny')")
    host = PythonHost()
    assert run(host, ctx, data).action == "allow"
    assert ctx.hook_input["tool_input"] == {"x": [1]}
    assert host.status("custom") == "degraded"


def test_registry_never_executes_discovery_and_rechecks_same_stat_bytes(ctx):
    import os

    path = ctx.paths.config.parent / "rules" / "custom.py"
    path.parent.mkdir()
    data = source(init="raise RuntimeError('must not import during discovery')")
    path.write_bytes(data)
    config = replace(
        ctx,
        config=Config(
            rules=Rules(approved={"custom": hashlib.sha256(data).hexdigest()}, enabled=["custom"])
        ),
    ).config
    registry = RuleRegistry(ctx.paths)
    entry = next(e for e in registry.entries(config) if e.name == "custom")
    assert entry.active and entry.snapshot == data
    stamp = path.stat()
    path.write_bytes(data.replace(b"blocked", b"changed"))
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    entry = next(e for e in registry.entries(config) if e.name == "custom")
    assert entry.status == "stale" and not entry.active


def test_syntax_error_is_invalid_without_initialization(ctx):
    path = ctx.paths.config.parent / "rules" / "custom.py"
    path.parent.mkdir()
    path.write_bytes(source() + b"\nreturn broken\n")
    entry = next(e for e in RuleRegistry(ctx.paths).entries(Config()) if e.name == "custom")
    assert entry.status == "invalid" and entry.error


def test_one_inflight_per_rule_and_no_queued_workers(ctx, monkeypatch):
    gate, entered = threading.Event(), threading.Event()
    monkeypatch.setattr(threading, "_wd143_gate", gate, raising=False)
    monkeypatch.setattr(threading, "_wd143_entered", entered, raising=False)
    data = source(
        "import threading; threading._wd143_entered.set(); "
        "threading._wd143_gate.wait(); return Decision(action='deny')",
        timeout=5000,
    )
    host, answers = PythonHost(max_workers=1), []
    caller = threading.Thread(target=lambda: answers.append(run(host, ctx, data)))
    caller.start()
    try:
        assert entered.wait(3)
        assert run(host, ctx, data).action == "allow"
        assert run(host, ctx, data.replace(b"'custom'", b"'other'")).action == "allow"
    finally:
        gate.set()
        caller.join(5)
    assert not caller.is_alive()
    assert len(answers) == 1 and answers[0].action == "deny"


def test_approved_snapshot_survives_path_replacement_without_executing_new_bytes(ctx, tmp_path):
    marker = tmp_path / "unapproved"
    path = ctx.paths.config.parent / "rules" / "custom.py"
    path.parent.mkdir()
    data = source()
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    config = Config(rules=Rules(approved={"custom": digest}, enabled=["custom"]))
    entry = next(e for e in RuleRegistry(ctx.paths).entries(config) if e.name == "custom")
    path.write_bytes(source(init=f"from pathlib import Path\nPath({str(marker)!r}).touch()"))
    assert isinstance(entry.rule, PythonRule) and entry.snapshot is not None
    result = PythonHost().evaluate(entry.rule, entry.snapshot, digest, ctx)
    assert result.action == "deny" and not marker.exists()
    assert (result.rule, result.rule_version) == ("custom", "1")


@pytest.mark.parametrize(
    "global_on,project_on,provider,profile",
    [
        (False, True, "claude", None),
        (True, False, "claude", None),
        (True, True, "claude", None),
        (True, True, "codex", None),
        (True, True, "codex", "windows-cli-0.160.0"),
    ],
)
def test_engine_keeps_switches_profile_gates_and_attribution(
    ctx, tmp_path, monkeypatch, global_on, project_on, provider, profile
):
    from uuid import uuid4

    from agent_watchdog.config import Limits, Overrides, Project
    from agent_watchdog.rules import api, engine

    monkeypatch.setattr(api, "_WINDOWS", True)

    marker = tmp_path / "initialized"
    data = source(init=f"from pathlib import Path\nPath({str(marker)!r}).touch()")
    data = data.replace(b"'claude'", repr(provider).encode())
    path = ctx.paths.config.parent / "rules" / "custom.py"
    path.parent.mkdir()
    path.write_bytes(data)
    project = Project(id=uuid4(), root=tmp_path, overrides=Overrides(policy_intervene=project_on))
    config = Config(
        defaults=Limits(policy_intervene=global_on),
        rules=Rules(
            approved={"custom": hashlib.sha256(data).hexdigest()},
            enabled=["custom"],
            disabled=["destructive_command"],
        ),
    )
    monkeypatch.setattr(engine, "_project_of", lambda *_: project)
    answer = engine.decide(
        ctx.paths,
        config,
        provider,
        {"hook_event_name": "PreToolUse", "tool_name": "Bash"},
        codex_control_profile=profile,
    )
    assert marker.exists() == (global_on and project_on)
    expected = "log" if provider == "codex" and profile is None else "deny"
    assert answer.action == (expected if global_on and project_on else "allow")
    if answer.action == "deny":
        assert answer.project_id == project.id
        assert (answer.rule, answer.rule_version) == ("custom", "1")


def test_python_approval_does_not_enable_and_duplicate_names_fail_closed(ctx):
    import tomli_w

    path = ctx.paths.config.parent / "rules" / "custom.py"
    path.parent.mkdir()
    data = source()
    path.write_bytes(data)
    config = Config(rules=Rules(approved={"custom": hashlib.sha256(data).hexdigest()}))
    entry = next(e for e in RuleRegistry(ctx.paths).entries(config) if e.name == "custom")
    assert entry.status == "disabled" and not entry.active
    metadata = parse_python(data).model_dump(exclude={"timeout_ms"}, exclude_none=True)
    path.with_suffix(".toml").write_text(tomli_w.dumps(metadata), encoding="utf-8")
    entries = [e for e in RuleRegistry(ctx.paths).entries(config) if e.name == "custom"]
    assert len(entries) == 2 and all(e.status == "invalid" for e in entries)


def test_undeclared_decisions_and_diagnostic_failure_fail_open(ctx):
    def broken_diagnostic(*_):
        raise OSError("log unavailable")

    host = PythonHost()
    answer = run(
        host,
        ctx,
        source("return Decision(action='context', context='wrong action')"),
        on_error=broken_diagnostic,
    )
    assert answer.action == "allow" and host.status("custom") == "degraded"


def test_timeout_diagnostic_is_not_hidden_by_an_earlier_rule_exception(ctx, monkeypatch):
    from types import SimpleNamespace

    from agent_watchdog import daemon

    logs = []
    monkeypatch.setattr(daemon, "_log", lambda *args, **fields: logs.append(fields))
    server = SimpleNamespace(watchdog_failed_rules=set(), watchdog_paths=ctx.paths)
    for error in (ValueError(), TimeoutError(), PythonRuleTimeout(), PythonRuleTimeout()):
        daemon._PolicyRequestHandler._rule_failed(
            cast(daemon._PolicyServer, server), "custom", error
        )
    assert [log["error_type"] for log in logs] == ["valueerror", "pythonruletimeout"]
