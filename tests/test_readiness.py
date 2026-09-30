"""WD-120: end-to-end collection readiness, distinct from `doctor`'s storage/daemon health."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog import daemon, readiness
from agent_watchdog.config import Config, Project, UserPaths, load_config, save_config
from agent_watchdog.daemon import _drain_spool, set_desired
from agent_watchdog.hook_install import change
from agent_watchdog.storage import Inbox, Store, writer_lock

RUNNING = {"state": "running", "alive": True, "queues": {}, "losses": {}}


@pytest.fixture
def rust_adapter():
    suffix = ".exe" if os.name == "nt" else ""
    installed = os.environ.get("WATCHDOG_NATIVE_ADAPTER")
    binary = (
        Path(installed)
        if installed
        else Path(__file__).parents[1]
        / "native"
        / "target"
        / "release"
        / f"agent-watchdog-hook{suffix}"
    )
    assert binary.is_file(), (
        "Set WATCHDOG_NATIVE_ADAPTER to an installed adapter or build it with "
        "cargo build --release --manifest-path native/Cargo.toml"
    )
    return binary


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    project = Project(id=uuid4(), root=root)
    save_config(paths.config, Config(projects=(project,)))
    paths.data.mkdir()
    # Hold the daemon lock so the adapter never launches a real core; tests drive the
    # drain and the purge themselves, exactly as tests/test_rust_adapter.py does.
    with writer_lock(paths.data / "daemon.lock"):
        yield paths, project


@pytest.fixture(params=["codex", "claude"])
def provider(request):
    return request.param


@pytest.fixture
def installed(setup, rust_adapter, provider, tmp_path):
    paths, _ = setup
    name = "hooks.json" if provider == "codex" else "settings.json"
    target = tmp_path / "provider" / name
    change(
        target,
        paths,
        install=True,
        apply=True,
        adapter_executable=rust_adapter,
        provider=provider,
    )
    return target


def stages(report):
    return {name: stage["state"] for name, stage in report["stages"].items()}


def purge_directly(paths, project):
    """Stand in for the core's control-request handler, which needs a live daemon."""

    def purge(request):
        with Store(paths.project_data(project.id), project.id) as store:
            return {
                "deleted_events": store.purge_provider_session(
                    request["provider"], request["session_id"]
                )
            }

    return purge


def drain_everything(paths):
    """Run the daemon's spool drain plus the inbox commit once, as `_poll` would."""

    def settle():
        _drain_spool(paths, load_config(paths.config))
        for project in load_config(paths.config).projects:
            root = paths.project_data(project.id)
            if (root / "inbox").is_dir():
                with Store(root, project.id) as store:
                    Inbox(root).drain(store)

    return settle


def test_absent_hook_fails_the_hook_stage_and_skips_the_rest(setup, provider, tmp_path):
    paths, _ = setup
    name = "hooks.json" if provider == "codex" else "settings.json"
    report = readiness.check(paths, provider, tmp_path / name, project_ref=None)
    assert report["probe"] == "none" and not report["ok"]
    assert stages(report)["hook"] == "failed"
    assert report["stages"]["hook"]["evidence"]["reason"] == "hook_file_missing"
    assert report["stages"]["hook"]["action"]
    assert {stages(report)[key] for key in ("spool", "admission")} == {"unknown"}


def test_a_hook_file_without_an_ownership_record_fails(setup, provider, tmp_path):
    paths, _ = setup
    name = "hooks.json" if provider == "codex" else "settings.json"
    target = tmp_path / name
    target.write_text('{"hooks": {}}')
    report = readiness.check(paths, provider, target, project_ref=None)
    assert report["stages"]["hook"]["evidence"]["reason"] == "ownership_record_missing"
    assert not report["ok"]


def test_installed_hook_reports_a_usable_native_artifact(setup, installed, provider, rust_adapter):
    paths, project = setup
    report = readiness.check(paths, provider, installed, project_ref="project")
    hook = report["stages"]["hook"]
    assert hook["state"] == "ready"
    assert hook["evidence"]["executable"] == str(rust_adapter)
    assert hook["evidence"]["adapter_version"].startswith("agent-watchdog-hook ")
    # Trust cannot be observed and no callback has arrived: never `ready` by inference.
    callback = report["stages"]["provider_callback"]
    assert callback["state"] == "unknown"
    assert callback["evidence"]["native_trust"] == "not_inspected"


def test_passive_check_never_claims_delivery_and_leaves_state_untouched(
    setup, installed, provider, monkeypatch
):
    paths, project = setup
    monkeypatch.setattr(daemon, "status", lambda _paths: RUNNING)
    before = installed.read_bytes()
    report = readiness.check(paths, provider, installed, project_ref="project")
    assert stages(report) == {
        "hook": "ready",
        "project": "ready",
        "provider_callback": "unknown",
        "spool": "unknown",
        "admission": "unknown",
    }
    assert report["ok"] and installed.read_bytes() == before
    assert not (paths.data / "spool").exists()
    assert not paths.project_data(project.id).exists()


@pytest.mark.parametrize(
    "damage, reason",
    [
        ("delete_executable", "executable_missing"),
        ("edit_owned_group", "owned_hook_edited"),
        ("drop_owned_group", "owned_hook_missing"),
        ("other_instance", "hook_targets_another_instance"),
    ],
)
def test_broken_hook_fails_with_a_specific_reason(
    setup, provider, tmp_path, rust_adapter, damage, reason
):
    paths, _ = setup
    copy = tmp_path / ("adapter.exe" if os.name == "nt" else "adapter")
    copy.write_bytes(rust_adapter.read_bytes())
    copy.chmod(0o755)
    name = "hooks.json" if provider == "codex" else "settings.json"
    target = tmp_path / "provider" / name
    change(target, paths, install=True, apply=True, adapter_executable=copy, provider=provider)
    check_paths = paths
    if damage == "delete_executable":
        copy.unlink()
    elif damage == "edit_owned_group":
        document = json.loads(target.read_text())
        document["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 30
        target.write_text(json.dumps(document))
    elif damage == "drop_owned_group":
        document = json.loads(target.read_text())
        del document["hooks"]["Stop"]
        target.write_text(json.dumps(document))
    else:
        check_paths = UserPaths(paths.config, tmp_path / "elsewhere", paths.runtime)
    before = target.read_bytes()
    report = readiness.check(check_paths, provider, target, project_ref=None)
    assert stages(report)["hook"] == "failed" and not report["ok"]
    assert report["stages"]["hook"]["evidence"]["reason"] == reason
    assert target.read_bytes() == before


def test_python_fallback_command_is_not_a_native_artifact(setup, provider, tmp_path):
    paths, _ = setup
    name = "hooks.json" if provider == "codex" else "settings.json"
    target = tmp_path / name
    change(target, paths, install=True, apply=True, provider=provider)
    report = readiness.check(paths, provider, target, project_ref=None)
    assert report["stages"]["hook"]["evidence"]["reason"] == "python_fallback_command"
    assert stages(report)["hook"] == "failed"


def test_unresolved_project_fails_and_blocks_the_probe(setup, installed, provider, monkeypatch):
    paths, _ = setup
    monkeypatch.setattr(daemon, "status", lambda _paths: RUNNING)
    report = readiness.check(
        paths, provider, installed, project_ref="no-such-project", probe=True, timeout=0.2
    )
    assert stages(report)["project"] == "failed" and not report["ok"]
    assert stages(report)["spool"] == stages(report)["admission"] == "unknown"
    assert report["probe"] == "none"
    assert not (paths.data / "spool").exists() or not list((paths.data / "spool").glob("*.json"))


def test_paused_daemon_fails_delivery_because_the_adapter_drops_events(
    setup, installed, provider, monkeypatch
):
    paths, project = setup
    set_desired(paths, paused=True)
    monkeypatch.setattr(daemon, "status", lambda _paths: {**RUNNING, "state": "paused"})
    passive = readiness.check(paths, provider, installed, project_ref="project")
    assert stages(passive)["spool"] == stages(passive)["admission"] == "failed"
    assert passive["stages"]["spool"]["evidence"]["reason"] == "daemon_paused"
    probed = readiness.check(
        paths, provider, installed, project_ref="project", probe=True, timeout=0.2
    )
    assert probed["probe"] == "none"  # nothing was sent while paused
    assert not (paths.data / "spool").exists()


def test_unavailable_daemon_is_unknown_not_failed(setup, installed, provider, monkeypatch):
    paths, _ = setup
    monkeypatch.setattr(
        daemon, "status", lambda _paths: {"state": "unavailable", "alive": False, "queues": {}}
    )
    report = readiness.check(paths, provider, installed, project_ref="project")
    assert stages(report)["admission"] == "unknown"
    assert report["stages"]["admission"]["evidence"]["daemon"] == "unavailable"
    assert report["ok"]


def test_probe_delivers_through_the_installed_adapter_and_removes_its_traces(
    setup, installed, provider, monkeypatch
):
    paths, project = setup
    monkeypatch.setattr(daemon, "status", lambda _paths: RUNNING)
    before = installed.read_bytes()
    report = readiness.check(
        paths,
        provider,
        installed,
        project_ref="project",
        probe=True,
        settle=drain_everything(paths),
        purge=purge_directly(paths, project),
    )
    assert report["probe"] == "synthetic-local" and report["ok"]
    assert stages(report) == {
        "hook": "ready",
        "project": "ready",
        "provider_callback": "unknown",  # a synthetic probe is never provider evidence
        "spool": "ready",
        "admission": "ready",
    }
    admission = report["stages"]["admission"]["evidence"]
    assert admission["synthetic"] is True and admission["cleanup"] == "purged"
    assert report["stages"]["spool"]["evidence"]["synthetic"] is True
    with Store(paths.project_data(project.id), project.id) as store:
        assert store.events() == []
    assert installed.read_bytes() == before


def test_probe_without_a_draining_core_reports_spool_ready_and_admission_failed(
    setup, installed, provider, monkeypatch
):
    paths, _ = setup
    monkeypatch.setattr(daemon, "status", lambda _paths: RUNNING)
    report = readiness.check(
        paths, provider, installed, project_ref="project", probe=True, timeout=0.3
    )
    assert stages(report)["spool"] == "ready"
    assert stages(report)["admission"] == "failed"
    assert report["stages"]["admission"]["evidence"]["reason"] == "not_admitted_in_time"
    assert not report["ok"]


def test_probe_records_an_adapter_failure_instead_of_claiming_delivery(
    setup, installed, provider, monkeypatch
):
    paths, _ = setup
    monkeypatch.setattr(daemon, "status", lambda _paths: RUNNING)
    (paths.data / "spool").write_text("not a directory")  # the adapter cannot spool
    report = readiness.check(
        paths, provider, installed, project_ref="project", probe=True, timeout=0.3
    )
    assert stages(report)["spool"] == "failed" and stages(report)["admission"] == "unknown"


def test_callback_history_is_ready_only_for_real_events_after_the_install(
    setup, installed, provider, rust_adapter, monkeypatch
):
    paths, project = setup
    monkeypatch.setattr(daemon, "status", lambda _paths: RUNNING)
    subprocess.run(
        [
            str(rust_adapter),
            "--python",
            sys.executable,
            "--config",
            str(paths.config),
            "--data",
            str(paths.data),
            "--runtime",
            str(paths.runtime),
            "hook",
            provider,
        ],
        input=json.dumps(
            {"cwd": str(project.root), "session_id": "real", "hook_event_name": "SessionStart"}
        ),
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    drain_everything(paths)()
    report = readiness.check(paths, provider, installed, project_ref="project")
    callback = report["stages"]["provider_callback"]
    assert callback["state"] == "ready"
    assert callback["evidence"]["events"] == 1 and callback["evidence"]["native_trust"] == (
        "not_inspected"
    )
