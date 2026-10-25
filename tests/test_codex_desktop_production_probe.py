"""Offline contracts for the operator-driven Codex desktop production-adapter probe (WD-151)."""

import importlib
import json
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_watchdog.config import load_config
from agent_watchdog.daemon import _policy_server, _SessionTracker
from agent_watchdog.rules.api import CODEX_CELLS
from agent_watchdog.rules.declarative import parse_rule
from agent_watchdog.storage import writer_lock

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
try:
    probe = importlib.import_module("codex_desktop_production_probe")
finally:
    sys.path.pop(0)

BANNER = "agent-watchdog-hook 0.1.5 (providers: codex, claude; decision schema 2)"


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    path = tmp_path / "adapter.exe"
    path.write_bytes(b"synthetic")
    monkeypatch.setattr(probe, "adapter_version", lambda _path: BANNER)
    return path


@pytest.fixture
def scratch(tmp_path):
    return tmp_path / "scratch"


def hook_definition(info):
    return json.loads((Path(info["dir"]) / ".codex" / "hooks.json").read_text(encoding="utf-8"))


def test_profile_is_the_exact_supported_desktop_profile():
    assert probe.PROFILE in CODEX_CELLS
    assert probe.PROFILE.startswith("windows-desktop-")


@pytest.mark.parametrize("case", sorted(probe.CASES))
def test_prepare_approves_only_the_scratch_rule_for_the_registered_checkout(
    scratch, adapter, case, tmp_path
):
    info = probe.prepare(scratch, case, adapter)
    work = Path(info["dir"])
    config = load_config(probe.paths_at(scratch).config)
    assert [project.root.resolve() for project in config.projects] == [work.resolve()]
    assert set(config.rules.approved) == {probe.RULE_NAME}
    assert config.defaults.policy_intervene is True
    # Nothing is created outside the scratch tree, and the checkout is a Git repository.
    assert all(scratch in path.parents or path == scratch for path in scratch.rglob("*"))
    assert (work / ".git").exists()
    assert not (work / probe.SENTINEL).exists()
    assert info["case"] == case and info["prompt"] and info["steps"]
    rule = parse_rule(
        (probe.paths_at(scratch).config.parent / "rules" / probe.RULE_FILE).read_text()
    )
    spec = probe.CASES[case]
    assert (rule.event, rule.action.kind, rule.match.tool_name) == (
        spec.event,
        spec.action,
        "Bash",
    )
    assert tmp_path in scratch.parents


@pytest.mark.parametrize("case", sorted(probe.CASES))
def test_hook_command_uses_the_production_adapter_and_the_exact_profile(scratch, adapter, case):
    info = probe.prepare(scratch, case, adapter)
    definition = hook_definition(info)
    spec = probe.CASES[case]
    assert set(definition["hooks"]) == {"PreToolUse", "PermissionRequest"}
    for event, groups in definition["hooks"].items():
        assert groups[0]["matcher"] == "^Bash$"
        handler = groups[0]["hooks"][0]
        command = handler["commandWindows"]
        assert str(adapter) in command
        assert "hook" in command and "codex" in command
        assert "relay" not in command  # the production adapter is the hook, with no wrapper
        assert ("--codex-control-profile" in command) is spec.with_profile
        if spec.with_profile:
            assert probe.PROFILE in command
        assert handler["timeout"] == 30, event


def test_negative_case_installs_the_same_rule_without_a_profile(scratch, adapter):
    info = probe.prepare(scratch, "no-profile-deny", adapter)
    assert "--codex-control-profile" not in json.dumps(hook_definition(info))
    assert probe.CASES["no-profile-deny"].expect == "silent"


@pytest.mark.parametrize("case", ["permission-deny", "permission-permit"])
def test_permission_prompts_ask_for_an_escalated_scratch_command(scratch, adapter, case):
    assert "require_escalated" in probe.prepare(scratch, case, adapter)["prompt"]


def test_permit_rule_matches_the_whole_scratch_command_only(scratch, adapter):
    probe.prepare(scratch, "permission-permit", adapter)
    path = probe.paths_at(scratch).config.parent / "rules" / probe.RULE_FILE
    rule = parse_rule(path.read_text())
    import re

    pattern = rule.match.input_regex
    assert pattern is not None
    assert re.fullmatch(pattern, f'powershell -Command "Set-Content {probe.SENTINEL} x"')
    assert not re.fullmatch(pattern, "Remove-Item -Recurse C:\\")


def test_prepare_refuses_a_used_scratch_and_a_bad_adapter(scratch, adapter, monkeypatch):
    probe.prepare(scratch, "permission-deny", adapter)
    with pytest.raises(ValueError, match="new or empty"):
        probe.prepare(scratch, "permission-deny", adapter)
    monkeypatch.setattr(
        probe,
        "adapter_version",
        lambda _path: (_ for _ in ()).throw(ValueError("lacks the required decision schema")),
    )
    with pytest.raises(ValueError, match="decision schema"):
        probe.prepare(scratch.with_name("other"), "permission-deny", adapter)


def test_prepare_rejects_a_scratch_inside_the_repository():
    root = Path(__file__).resolve().parents[1]
    with pytest.raises(ValueError, match="outside"):
        probe.prepare(root / "tmp-wd151-scratch", "permission-deny", Path(__file__))


def control(action, delivered_at, ids, rule=probe.RULE_NAME):
    return SimpleNamespace(
        kind="control",
        provider="codex",
        source="daemon",
        payload={
            "codex": {
                "action": action,
                "rule": rule,
                "delivered_at": delivered_at,
                "evidence_ids": ids,
                "codex_control_profile": probe.PROFILE,
            }
        },
    )


def test_only_delivered_approved_controls_linked_to_the_callback_count():
    ids = {"evidence"}
    stamp = "2026-10-24T00:00:00Z"
    records = [
        control("log", None, ["evidence"]),
        control("deny", stamp, ["other"]),
        control("deny", None, ["evidence"]),
        control("deny", stamp, ["evidence"], rule="other_rule"),
        control("permit", stamp, ["evidence"]),
    ]
    assert probe.confirmed_controls(records, ids, "deny") == 0
    assert probe.confirmed_controls(records, ids, "permit") == 1
    records.append(control("deny", stamp, ["evidence"]))
    assert probe.confirmed_controls(records, ids, "deny") == 1


def facts(**overrides):
    base = {
        "callbacks": 1,
        "controls": 0,
        "sentinel": False,
        "dialog_seen": None,
        "app_version": probe.APP_VERSION,
        "backend_version": probe.BACKEND,
    }
    return probe.Facts(**{**base, **overrides})


def test_the_provisional_target_pins_its_own_versions_and_has_no_permit_case():
    probed, provisional = probe.TARGETS["probed"], probe.TARGETS["provisional"]
    assert provisional.profile.startswith("windows-desktop-26.1021.31345-")
    assert provisional.profile != probed.profile
    assert provisional.profile in CODEX_CELLS
    assert "permission-permit" not in provisional.cases
    assert set(provisional.cases) < set(probe.CASES)


@pytest.mark.parametrize("case", ["bash-deny", "permission-deny", "no-profile-deny"])
def test_the_provisional_target_embeds_its_profile(scratch, adapter, case):
    info = probe.prepare(scratch, case, adapter, target="provisional")
    command = json.dumps(hook_definition(info))
    profile = probe.TARGETS["provisional"].profile
    assert (profile in command) is probe.CASES[case].with_profile
    assert probe.PROFILE not in command


def test_the_provisional_target_refuses_the_permit_case(scratch, adapter):
    with pytest.raises(ValueError, match="not available"):
        probe.prepare(scratch, "permission-permit", adapter, target="provisional")


def test_a_pair_other_than_the_target_is_never_confirmed():
    provisional = probe.TARGETS["provisional"]
    matching = facts(
        controls=1, app_version=provisional.app_version, backend_version=provisional.backend
    )
    assert probe.evaluate("bash-deny", matching, target="provisional")[0] == "supported"
    assert probe.evaluate("bash-deny", matching)[0] == "inconclusive"  # the probed pair differs
    for stale in (None, probe.BACKEND):
        wrong = facts(controls=1, backend_version=stale)
        assert probe.evaluate("bash-deny", wrong, target="provisional")[0] == "inconclusive"
    assert probe.evaluate("bash-deny", facts(controls=1, backend_version=None))[0] == "inconclusive"


@pytest.mark.parametrize(
    "case,observed,verdict",
    [
        # Deny cells: a delivered control and an absent side effect are both required.
        ("bash-deny", facts(controls=1, sentinel=False), "supported"),
        ("bash-deny", facts(controls=1, sentinel=True), "unsupported"),
        ("bash-deny", facts(controls=0, sentinel=False), "inconclusive"),
        ("bash-deny", facts(callbacks=0), "inconclusive"),
        ("permission-deny", facts(controls=1, sentinel=False), "supported"),
        ("permission-deny", facts(controls=1, sentinel=True), "unsupported"),
        # Permit: the side effect must happen and the operator must have seen no dialog.
        ("permission-permit", facts(controls=1, sentinel=True, dialog_seen=False), "supported"),
        ("permission-permit", facts(controls=1, sentinel=True, dialog_seen=True), "inconclusive"),
        ("permission-permit", facts(controls=1, sentinel=True, dialog_seen=None), "inconclusive"),
        ("permission-permit", facts(controls=1, sentinel=False, dialog_seen=False), "unsupported"),
        ("permission-permit", facts(controls=0, sentinel=True, dialog_seen=False), "inconclusive"),
        # Negative: without the profile nothing may be delivered and the command runs.
        ("no-profile-deny", facts(controls=0, sentinel=True), "supported"),
        ("no-profile-deny", facts(controls=1, sentinel=False), "unsupported"),
        ("no-profile-deny", facts(callbacks=0, sentinel=True), "inconclusive"),
        # Another app version cannot confirm a cell scoped to the probed one.
        ("bash-deny", facts(controls=1, app_version="26.1100.1"), "inconclusive"),
    ],
)
def test_verdicts_require_both_the_stored_control_and_the_provider_effect(case, observed, verdict):
    assert probe.evaluate(case, observed)[0] == verdict


def test_collect_disables_the_owned_hook_and_never_confirms_without_a_daemon_record(
    scratch, adapter, tmp_path
):
    info = probe.prepare(scratch, "bash-deny", adapter)
    work = Path(info["dir"])
    record = probe.collect(scratch, desktop_version=probe.APP_VERSION, dialog_seen=None)
    assert record["verdict"] == "inconclusive"
    assert not (work / ".codex" / "hooks.json").exists()
    assert (work / ".codex" / "hooks.json.wd151-disabled").exists()
    assert record["case"] == "bash-deny" and record["profile"] == probe.PROFILE
    assert record["observed"]["callbacks"] == 0
    # A second collection must not claim anything new from a disabled hook.
    assert (
        probe.collect(scratch, desktop_version=probe.APP_VERSION, dialog_seen=None)["verdict"]
        == "inconclusive"
    )


def test_collect_flags_an_edited_hook_and_a_running_daemon(scratch, adapter):
    info = probe.prepare(scratch, "bash-deny", adapter)
    hook = Path(info["dir"]) / ".codex" / "hooks.json"
    hook.write_text(hook.read_text() + " ", encoding="utf-8")
    record = probe.collect(scratch, desktop_version=probe.APP_VERSION, dialog_seen=None)
    assert any("hook" in problem for problem in record["problems"])
    assert hook.exists()  # an edited hook is left for manual inspection
    paths = probe.paths_at(scratch)
    with writer_lock(paths.data / "daemon.lock"), pytest.raises(ValueError, match="serve"):
        probe.collect(scratch, desktop_version=probe.APP_VERSION, dialog_seen=None)


def test_collect_requires_a_numeric_desktop_version(scratch, adapter):
    probe.prepare(scratch, "bash-deny", adapter)
    with pytest.raises(ValueError, match="desktop app version"):
        probe.collect(scratch, desktop_version="latest", dialog_seen=None)


@pytest.mark.skipif(sys.platform != "win32", reason="versioned Windows control profile")
@pytest.mark.parametrize(
    "case,event,action",
    [
        ("bash-deny", "PreToolUse", "deny"),
        ("permission-deny", "PermissionRequest", "deny"),
        ("permission-permit", "PermissionRequest", "permit"),
    ],
)
def test_native_adapter_decision_round_trips_through_the_scratch_store(
    rust_adapter, scratch, case, event, action, monkeypatch
):
    info = probe.prepare(scratch, case, rust_adapter)
    work = Path(info["dir"])
    paths = probe.paths_at(scratch)
    command = f"Set-Content -Path {probe.SENTINEL} -Value WD151_SIDE_EFFECT"
    transcript = scratch / "rollout.jsonl"
    transcript.write_text(
        json.dumps({"type": "session_meta", "payload": {"cli_version": probe.BACKEND}}) + "\n",
        encoding="utf-8",
    )
    payload = {
        "cwd": str(work),
        "session_id": "scratch-session",
        "hook_event_name": event,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "transcript_path": str(transcript),
    }
    hook_args = [
        str(rust_adapter),
        "--python",
        sys.executable,
        "--config",
        str(paths.config),
        "--data",
        str(paths.data),
        "--runtime",
        str(paths.runtime),
        "--codex-control-profile",
        probe.PROFILE,
        "hook",
        "codex",
    ]
    with writer_lock(paths.data / "daemon.lock"), _policy_server(paths, _SessionTracker()):
        result = subprocess.run(
            hook_args, input=json.dumps(payload).encode(), capture_output=True, timeout=30
        )
        # The daemon records its control after answering; wait for the hook record and the
        # control record before the server stops, so a loaded host cannot lose the latter.
        deadline = time.monotonic() + 10
        while len(list((paths.data / "spool").glob("*.json"))) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
    output = json.loads(result.stdout)
    assert output != {}
    # The command was never run by this test, so the sentinel is genuinely absent.
    record = probe.collect(
        scratch,
        desktop_version=probe.APP_VERSION,
        dialog_seen=False if action == "permit" else None,
    )
    assert record["observed"]["callbacks"] >= 1
    assert record["observed"]["controls"] == 1
    assert record["backend_version"] == probe.BACKEND
    # Deny is supported (absent effect); permit lacks its side effect, so it is not "supported".
    assert record["verdict"] == ("supported" if action == "deny" else "unsupported")
