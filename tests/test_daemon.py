import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent_watchdog.analysis import CheckoutUnknown
from agent_watchdog.config import (
    Config,
    Limits,
    Overrides,
    Project,
    UserPaths,
    load_config,
    save_config,
)
from agent_watchdog.daemon import (
    _capture_diff_snapshot,
    _ConfigCache,
    _drain_spool,
    _policy_server,
    _record_enrichment_failures,
    enqueue,
    launch,
    mutate_registry,
    start,
    status,
    stop,
    write_spool_limits,
)
from agent_watchdog.events import Envelope
from agent_watchdog.registry import Registry
from agent_watchdog.resources import losses
from agent_watchdog.rules.engine import decide
from agent_watchdog.storage import Inbox, StorageError, Store, WriterBusy, writer_lock
from agent_watchdog.transcripts import FAILURE_CODES


def spool_record(cwd, **input_fields):
    return {
        "schema_version": 1,
        "event_id": str(uuid4()),
        "received_at": datetime.now(UTC).isoformat(),
        "provider": "codex",
        "cwd": str(cwd),
        "input": input_fields,
    }


def write_spool_record(paths, record):
    directory = paths.data / "spool"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{uuid4().hex}.json").write_text(json.dumps(record), encoding="utf-8")


def drain_inbox(paths, project, config):
    root = paths.project_data(project.id)
    limits = project.overrides.apply(config.defaults)
    with Store(root, project.id, limits=limits) as store:
        return Inbox(root, limits=limits).drain(store)


def test_spool_record_is_resolved_built_and_admitted(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    record = spool_record(
        root,
        hook_event_name="UserPromptSubmit",
        session_id="s1",
        prompt="explain code; password=private-value",
    )
    write_spool_record(paths, record)
    inbox = paths.project_data(project.id) / "inbox"

    assert _drain_spool(paths, config) is True
    assert not list((paths.data / "spool").glob("*.json"))
    incoming = list(inbox.glob("*.json"))
    assert len(incoming) == 1
    envelope = Envelope.model_validate_json(incoming[0].read_bytes())
    assert str(envelope.event_id) == record["event_id"]
    assert envelope.kind == "turn.start" and envelope.session_id == "s1"
    resolved = Registry(config).resolve(root)
    assert resolved is not None and envelope.checkout_id == resolved.checkout_id
    assert b"private-value" in incoming[0].read_bytes()
    assert "explain code" in envelope.model_dump_json()


def test_spool_drain_preserves_enabled_delivery_trace_and_disabled_omits_it(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    record = spool_record(root, hook_event_name="Stop", session_id="s1")
    record["delivery"] = {
        "adapter_started_at": "2026-09-08T00:00:00+00:00",
        "spool_enqueued_at": "2026-09-08T00:00:01+00:00",
        "spool_occupancy": {"files": 0, "bytes": 0},
    }
    write_spool_record(paths, record)

    assert _drain_spool(paths, config)
    incoming = next((paths.project_data(project.id) / "inbox").glob("*.json"))
    traced = Envelope.model_validate_json(incoming.read_bytes())
    assert set(traced.delivery) == {
        "adapter_started_at",
        "spool_enqueued_at",
        "spool_occupancy",
        "spool_drained_at",
        "inbox_enqueued_at",
        "inbox_occupancy",
    }
    with Store(paths.project_data(project.id), project.id) as store:
        assert (
            Inbox(paths.project_data(project.id)).drain(store, pipeline_telemetry=True).inserted
            == 1
        )
        stored = store.events()[0]
    assert {"inbox_drained_at", "sqlite_write_started_at"} <= set(stored.delivery)

    disabled = config.model_copy(update={"pipeline_telemetry": False})
    write_spool_record(paths, record | {"event_id": str(uuid4())})
    assert _drain_spool(paths, disabled)
    incoming = next((paths.project_data(project.id) / "inbox").glob("*.json"))
    assert Envelope.model_validate_json(incoming.read_bytes()).delivery == {}


def test_spool_drain_records_a_debounced_content_free_diff_fingerprint(
    paths, tmp_path, monkeypatch
):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    monkeypatch.setattr(
        "agent_watchdog.analysis.git_diff_fingerprint", lambda checkout: ("a" * 64, 123)
    )
    write_spool_record(paths, spool_record(root, hook_event_name="Stop", session_id="s1"))

    assert _drain_spool(paths, config) is True
    with Store(paths.project_data(project.id), project.id) as store:
        rows = store.connection.execute(
            "SELECT fingerprint, byte_count FROM diff_snapshots"
        ).fetchall()
    assert rows == [("a" * 64, 123)]


UNKNOWN_REASONS = [
    "git_failed",
    "deadline",
    "diff_too_large",
    "too_many_untracked",
    "untracked_too_large",
    "untracked_unreadable",
]


def unknown_checkout_log(paths):
    log = paths.data / "watchdog.log"
    lines = log.read_text(encoding="ascii").splitlines() if log.exists() else []
    return [line for line in lines if "event=checkout" in line]


@pytest.mark.parametrize("reason", UNKNOWN_REASONS)
def test_spool_drain_logs_an_unknown_checkout_without_recording_a_snapshot(
    paths, tmp_path, monkeypatch, reason
):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    monkeypatch.setattr(
        "agent_watchdog.analysis.git_diff_fingerprint",
        lambda checkout: CheckoutUnknown(reason, f"{checkout} is private-detail"),
    )
    write_spool_record(paths, spool_record(root, hook_event_name="Stop", session_id="s1"))

    assert _drain_spool(paths, config) is True

    (line,) = unknown_checkout_log(paths)
    assert "level=WARNING component=daemon event=checkout decision=unavailable" in line
    assert f"error_type={reason} project_id={project.id}" in line
    assert "private-detail" not in line and str(root) not in line and "detail=" not in line
    with Store(paths.project_data(project.id), project.id) as store:
        assert store.connection.execute("SELECT COUNT(*) FROM diff_snapshots").fetchone() == (0,)


def test_unknown_checkout_is_logged_once_per_episode_and_reason(paths, tmp_path, monkeypatch):
    project_id = uuid4()
    config = Config(defaults=Limits(log_level="DEBUG"))
    outcomes = []
    monkeypatch.setattr(
        "agent_watchdog.analysis.git_diff_fingerprint", lambda checkout: outcomes.pop(0)
    )
    # A recorded snapshot would debounce the next read, which is not under test here.
    monkeypatch.setattr(Store, "diff_due", lambda self, checkout_id, *, now, **kwargs: True)
    logged: set[tuple[UUID, str]] = set()
    first, second = uuid4(), uuid4()

    def observe(checkout_id, outcome):
        outcomes.append(outcome)
        _capture_diff_snapshot(checkout_id, tmp_path, project_id, paths, config, logged)
        return len(unknown_checkout_log(paths))

    deadline = CheckoutUnknown("deadline")
    assert observe(first, deadline) == 1
    assert observe(first, deadline) == 1  # the same episode stays quiet
    assert observe(second, deadline) == 2  # another checkout is its own episode
    assert observe(first, CheckoutUnknown("diff_too_large")) == 3  # a new reason logs
    assert observe(first, ("a" * 64, 1)) == 3  # the state fits the bounds again
    assert observe(first, deadline) == 4  # and a later failure is a new episode
    assert observe(second, deadline) == 4  # the other checkout's episode was untouched


def test_unknown_checkout_detail_is_opt_in_and_carries_the_checkout_path(
    paths, tmp_path, monkeypatch
):
    config = Config(defaults=Limits(log_level="DEBUG", log_detail=True))
    monkeypatch.setattr(
        "agent_watchdog.analysis.git_diff_fingerprint",
        lambda checkout: CheckoutUnknown("untracked_unreadable", "locked by another process"),
    )

    _capture_diff_snapshot(uuid4(), tmp_path, uuid4(), paths, config, set())

    (line,) = unknown_checkout_log(paths)
    assert "error_type=untracked_unreadable" in line
    assert ' detail="' in line and str(tmp_path) in line and "locked by another process" in line


def test_spool_unregistered_cwd_is_discarded_without_loss(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config).model_copy(update={"defaults": Limits(log_level="DEBUG")})
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    write_spool_record(paths, spool_record(foreign, hook_event_name="Stop"))

    assert _drain_spool(paths, config) is True
    assert not list((paths.data / "spool").glob("*.json"))
    assert not list(paths.data.glob("projects/*/inbox/*.json"))
    assert losses(paths.data)["invalid"] == 0
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "component=daemon event=spool decision=unregistered" in body
    assert str(foreign) not in body


def test_invalid_spool_log_uses_a_fixed_reason_without_record_content(paths):
    config = Config(defaults=Limits(log_level="DEBUG"))
    secret = "prompt=private-value"
    paths.data.joinpath("spool").mkdir(parents=True)
    (paths.data / "spool" / "broken.json").write_text(secret, encoding="utf-8")

    assert _drain_spool(paths, config) is True

    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "component=daemon event=spool decision=discarded reason=invalid" in body
    assert secret not in body


def test_invalid_spool_log_names_the_error_category(paths):
    config = Config(defaults=Limits(log_level="DEBUG"))
    paths.data.joinpath("spool").mkdir(parents=True)
    (paths.data / "spool" / "broken.json").write_text("not json", encoding="utf-8")

    assert _drain_spool(paths, config) is True

    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert (
        "component=daemon event=spool decision=discarded reason=invalid error_type=jsondecodeerror"
        in body
    )


def test_log_detail_opt_in_adds_the_error_string_to_spool_failures(paths):
    config = Config(defaults=Limits(log_level="DEBUG", log_detail=True))
    paths.data.joinpath("spool").mkdir(parents=True)
    (paths.data / "spool" / "broken.json").write_text("not json", encoding="utf-8")

    assert _drain_spool(paths, config) is True

    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "reason=invalid error_type=jsondecodeerror" in body
    assert ' detail="' in body and "Expecting value" in body


def test_oversized_spool_record_is_discarded_with_a_size_reason(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config).model_copy(update={"defaults": Limits(log_level="DEBUG")})
    record = spool_record(root, hook_event_name="Stop", filler="x" * (1024**2 + 128))
    write_spool_record(paths, record)

    assert _drain_spool(paths, config) is True

    assert not list((paths.data / "spool").glob("*.json"))
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert "component=daemon event=spool decision=discarded reason=oversized" in body


def test_spool_trusted_git_project_is_auto_added_and_admitted(paths, tmp_path):
    trusted = tmp_path / "repos"
    repository = trusted / "new-project"
    repository.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repository), "init"], check=True, capture_output=True)
    config = Config(auto_add_projects=True, trusted_projects_dir=trusted)
    save_config(paths.config, config)
    write_spool_record(paths, spool_record(repository, hook_event_name="Stop"))

    assert _drain_spool(paths, config) is True
    persisted = load_config(paths.config)
    assert len(persisted.projects) == 1
    project = persisted.projects[0]
    assert project.root == repository.resolve()
    assert len(list((paths.project_data(project.id) / "inbox").glob("*.json"))) == 1


def test_spool_drain_enforces_the_project_payload_limit(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config).model_copy(update={"defaults": Limits(payload_bytes=4096)})
    project = config.projects[0]
    # ensure_ascii expansion of these code points pushes the canonical envelope
    # past 4096 bytes; the drain, not the adapter, rejects it here.
    write_spool_record(
        paths, spool_record(root, hook_event_name="UserPromptSubmit", prompt="界" * 600)
    )

    assert _drain_spool(paths, config) is True
    assert not list((paths.project_data(project.id) / "inbox").glob("*.json"))
    assert losses(paths.project_data(project.id))["payload"] == 1


def test_spool_redrain_is_idempotent(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    record = spool_record(root, hook_event_name="Stop")
    write_spool_record(paths, record)
    assert _drain_spool(paths, config) is True
    # A second identical record (a replayed spool file) must not duplicate the event.
    write_spool_record(paths, record)
    assert _drain_spool(paths, config) is True
    result = drain_inbox(paths, project, config)
    assert result.inserted == 1 and result.duplicates == 1
    with Store(paths.project_data(project.id), project.id) as store:
        assert [str(item.event_id) for item in store.events()] == [record["event_id"]]


def test_config_cache_skips_reparsing_an_unchanged_file(paths, monkeypatch, tmp_path):
    save_config(paths.config, Config())
    calls = []
    real_load_config = load_config

    def counting_load_config(path):
        calls.append(path)
        return real_load_config(path)

    monkeypatch.setattr("agent_watchdog.daemon.load_config", counting_load_config)
    cache = _ConfigCache()

    first = cache.load(paths.config)
    second = cache.load(paths.config)
    assert second is first
    assert len(calls) == 1

    save_config(paths.config, Config(pipeline_telemetry=False))
    third = cache.load(paths.config)
    assert third is not first
    assert third.pipeline_telemetry is False
    assert len(calls) == 2


def test_config_cache_keys_by_path_not_just_content(tmp_path):
    first_path = tmp_path / "one" / "config.toml"
    second_path = tmp_path / "two" / "config.toml"
    save_config(first_path, Config(pipeline_telemetry=True))
    save_config(second_path, Config(pipeline_telemetry=False))
    cache = _ConfigCache()
    assert cache.load(first_path).pipeline_telemetry is True
    assert cache.load(second_path).pipeline_telemetry is False


def test_write_spool_limits_uses_largest_project_payload(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    write_spool_limits(paths, config)
    published = json.loads((paths.data / "spool" / "limits.json").read_text())
    assert published["schema_version"] == 1
    assert published["payload_bytes"] == config.defaults.payload_bytes
    assert published["spool_files"] > 0 and published["spool_bytes"] > 0


def _seed_coordinator_usage(paths, project, *, session_id, model):
    with Store(paths.project_data(project.id), project.id) as store:
        store.put(
            Envelope(
                provider="claude",
                project_id=project.id,
                session_id=session_id,
                kind="usage",
                source="transcript",
                received_at=datetime.now(UTC),
                payload={"claude": {"model": model}},
            )
        )


def _agent_input(root, *, session_id="session-1", model="opus", **fields):
    tool_input = {"subagent_type": "general-purpose", "prompt": "p"}
    if model is not None:
        tool_input["model"] = model
    return {
        "cwd": str(root),
        "session_id": session_id,
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "tool_input": tool_input,
        **fields,
    }


def _decide(paths, hook_input, provider="claude"):
    return decide(paths, load_config(paths.config), provider, hook_input)


def test_decide_denies_a_same_family_subagent_spawn(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    _seed_coordinator_usage(paths, project, session_id="session-1", model="claude-opus-5-5")
    decision = _decide(paths, _agent_input(root, model="opus"))
    assert decision.action == "deny"
    assert decision.rule == "subagent_same_model"
    assert decision.project_id == project.id
    assert "(opus)" in decision.reason


def test_decide_respects_the_global_kill_switch(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    project = Project(id=uuid4(), root=root)
    save_config(
        paths.config,
        Config(
            defaults=Limits(policy_intervene_same_model_subagent_spawn=False), projects=(project,)
        ),
    )
    _seed_coordinator_usage(paths, project, session_id="session-1", model="claude-opus-5-5")
    assert _decide(paths, _agent_input(root, model="opus")).action == "allow"


def test_decide_respects_the_per_project_kill_switch(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    project = Project(
        id=uuid4(), root=root, overrides=Overrides(policy_intervene_same_model_subagent_spawn=False)
    )
    save_config(paths.config, Config(projects=(project,)))
    _seed_coordinator_usage(paths, project, session_id="session-1", model="claude-opus-5-5")
    assert _decide(paths, _agent_input(root, model="opus")).action == "allow"


def test_decide_allows_a_different_family_subagent_spawn(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    _seed_coordinator_usage(paths, project, session_id="session-1", model="claude-opus-5-5")
    assert _decide(paths, _agent_input(root, model="haiku")).action == "allow"


def test_decide_never_denies_the_lowest_tier(paths, tmp_path):
    """Denying asks the caller to downgrade the model; a haiku spawn already
    has nowhere lower to go, so it is never denied even when it matches the
    coordinator's own (also haiku) model."""
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    _seed_coordinator_usage(
        paths, project, session_id="session-1", model="claude-haiku-4-5-20251001"
    )
    assert _decide(paths, _agent_input(root, model="haiku")).action == "allow"


def test_decide_allows_when_cwd_is_unregistered(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert _decide(paths, _agent_input(other, model="opus")).action == "allow"


def test_decide_allows_when_no_coordinator_usage_observed(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    assert _decide(paths, _agent_input(root, model="opus")).action == "allow"


def _same_model_setup(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    _seed_coordinator_usage(paths, project, session_id="session-1", model="claude-opus-5-5")
    return root, project


@pytest.mark.parametrize(
    "variant",
    [
        pytest.param({"model": None}, id="no-model-override"),
        pytest.param({"tool_name": "Bash"}, id="other-tool"),
        pytest.param({"hook_event_name": "PostToolUse"}, id="other-event"),
        pytest.param({"session_id": None}, id="no-session"),
        pytest.param({"cwd": None}, id="no-cwd"),
    ],
)
def test_decide_only_judges_a_claude_agent_spawn_with_a_model(paths, tmp_path, variant):
    root, _ = _same_model_setup(paths, tmp_path)
    variant = dict(variant)
    model = variant.pop("model", "opus")
    hook_input = _agent_input(root, model=model, **variant)
    hook_input = {key: value for key, value in hook_input.items() if value is not None}
    assert _decide(paths, hook_input).action == "allow"


def test_decide_never_returns_an_action_for_codex(paths, tmp_path):
    """Nothing is returned to Codex without live evidence (WD-151)."""
    root, _ = _same_model_setup(paths, tmp_path)
    assert _decide(paths, _agent_input(root, model="opus"), provider="codex").action == "allow"


def test_decide_only_returns_actions_the_adapter_can_render(paths, tmp_path, monkeypatch):
    import agent_watchdog.rules.engine as engine
    from agent_watchdog.rules.api import Decision

    root, _ = _same_model_setup(paths, tmp_path)

    def block_everything(*_args):
        return Decision(action="block", rule="test", reason="x")

    monkeypatch.setattr(engine, "_RULES", (block_everything,))
    # `block` is renderable for Stop, but not for PreToolUse.
    assert _decide(paths, _agent_input(root)).action == "allow"
    assert _decide(paths, {"cwd": str(root), "hook_event_name": "Stop"}).action == "block"


def test_decide_never_blocks_a_stop_that_is_already_continuing(paths, tmp_path, monkeypatch):
    import agent_watchdog.rules.engine as engine
    from agent_watchdog.rules.api import Decision

    root, _ = _same_model_setup(paths, tmp_path)
    monkeypatch.setattr(
        engine, "_RULES", (lambda *_args: Decision(action="block", rule="test", reason="x"),)
    )
    hook_input = {"cwd": str(root), "hook_event_name": "Stop", "stop_hook_active": True}
    assert _decide(paths, hook_input).action == "allow"


def _policy_request(port, **fields):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.sendall((json.dumps(fields) + "\n").encode("utf-8"))
        connection.shutdown(socket.SHUT_WR)
        chunks = []
        while chunk := connection.recv(65536):
            chunks.append(chunk)
        return b"".join(chunks)


def _decision_request(discovery, root, event_id=None, **fields):
    return {
        "schema_version": 2,
        "token": discovery["token"],
        "provider": "claude",
        "event_id": str(event_id or uuid4()),
        "input": _agent_input(root, **fields),
    }


def _control_events(paths, project):
    inbox = paths.project_data(project.id) / "inbox"
    return [json.loads(path.read_text(encoding="utf-8")) for path in inbox.glob("*.json")]


def test_policy_server_answers_over_the_loopback_socket(paths, tmp_path):
    root, project = _same_model_setup(paths, tmp_path)
    socket_path = paths.data / "policy" / "socket.json"
    with _policy_server(paths):
        discovery = json.loads(socket_path.read_text(encoding="utf-8"))
        assert discovery["schema_version"] == 2
        assert discovery["timeout_ms"] == 300
        assert discovery["max_request_bytes"] > Limits().payload_bytes
        assert discovery["subscriptions"] == [
            {"provider": "claude", "hook_event_name": "PreToolUse", "tool_name": "Agent"}
        ]
        raw = _policy_request(discovery["port"], **_decision_request(discovery, root))
        answer = json.loads(raw)
        assert answer["schema_version"] == 2
        assert answer["action"] == "deny"
        assert "Downgrade" in answer["reason"]
    assert not socket_path.exists()


def test_policy_server_allows_with_a_bare_action(paths, tmp_path):
    root, _ = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        raw = _policy_request(
            discovery["port"], **_decision_request(discovery, root, model="haiku")
        )
    assert json.loads(raw) == {"schema_version": 2, "action": "allow"}


def test_policy_server_still_denies_a_spawn_with_a_very_long_prompt(paths, tmp_path):
    """The request carries the whole hook input; a long subagent prompt must
    not make the channel fail open and silently disable the rule."""
    root, _ = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        request = _decision_request(discovery, root)
        request["input"]["tool_input"]["prompt"] = "x" * (Limits().payload_bytes - 4096)
        raw = _policy_request(discovery["port"], **request)
    assert json.loads(raw)["action"] == "deny"


def test_policy_server_answers_nothing_beyond_the_request_budget(paths, tmp_path):
    root, _ = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        request = _decision_request(discovery, root)
        request["input"]["tool_input"]["prompt"] = "x" * discovery["max_request_bytes"]
        raw = _policy_request(discovery["port"], **request)
    assert raw == b""


def test_policy_server_records_a_control_event_for_a_delivered_action(paths, tmp_path):
    root, project = _same_model_setup(paths, tmp_path)
    event_id = uuid4()
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        raw = _policy_request(
            discovery["port"], **_decision_request(discovery, root, event_id=event_id)
        )
        assert json.loads(raw)["action"] == "deny"
        wait_for(lambda: _control_events(paths, project))
    (control,) = _control_events(paths, project)
    assert control["kind"] == "control"
    assert control["source"] == "daemon"
    assert control["provider"] == "claude"
    assert control["session_id"] == "session-1"
    assert control["project_id"] == str(project.id)
    recorded = control["payload"]["claude"]
    assert recorded["rule"] == "subagent_same_model"
    assert recorded["action"] == "deny"
    assert recorded["evidence_ids"] == [str(event_id)]
    assert "Downgrade" in recorded["reason"]


def test_a_control_event_is_stored_and_projected_like_any_other(paths, tmp_path):
    root, project = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        _policy_request(discovery["port"], **_decision_request(discovery, root))
        wait_for(lambda: _control_events(paths, project))
    with Store(paths.project_data(project.id), project.id) as store:
        Inbox(paths.project_data(project.id)).drain(store)
        row = store.connection.execute(
            "SELECT kind, source, provider, session_id FROM event_facts WHERE kind='control'"
        ).fetchone()
    assert tuple(row) == ("control", "daemon", "claude", "session-1")


def test_policy_server_records_no_control_event_for_allow(paths, tmp_path):
    root, project = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        _policy_request(discovery["port"], **_decision_request(discovery, root, model="haiku"))
        time.sleep(0.3)
    assert _control_events(paths, project) == []


def test_policy_server_still_answers_when_recording_the_control_event_fails(
    paths, tmp_path, monkeypatch
):
    import agent_watchdog.daemon as daemon_module

    def refuse(*_args, **_kwargs):
        raise StorageError("inbox full")

    monkeypatch.setattr(daemon_module, "_admit", refuse)
    root, project = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        raw = _policy_request(discovery["port"], **_decision_request(discovery, root))
        time.sleep(0.3)
    assert json.loads(raw)["action"] == "deny"
    assert _control_events(paths, project) == []


def test_policy_server_closes_without_an_answer_when_a_rule_raises(paths, tmp_path, monkeypatch):
    import agent_watchdog.daemon as daemon_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("rule bug")

    monkeypatch.setattr(daemon_module, "decide", explode)
    root, project = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        raw = _policy_request(discovery["port"], **_decision_request(discovery, root))
    assert raw == b""
    assert _control_events(paths, project) == []


def test_policy_server_closes_without_an_answer_when_the_config_is_unreadable(paths, tmp_path):
    root, _ = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        paths.config.write_text("this is = not [valid toml", encoding="utf-8")
        raw = _policy_request(discovery["port"], **_decision_request(discovery, root))
    assert raw == b""


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda request: request.update(schema_version=1), id="schema-1"),
        pytest.param(lambda request: request.update(schema_version=3), id="future-schema"),
        pytest.param(lambda request: request.pop("provider"), id="no-provider"),
        pytest.param(lambda request: request.update(provider="gemini"), id="unknown-provider"),
        pytest.param(lambda request: request.pop("input"), id="no-input"),
        pytest.param(lambda request: request.update(input="text"), id="input-not-an-object"),
        pytest.param(lambda request: request.pop("event_id"), id="no-event-id"),
        pytest.param(lambda request: request.update(event_id="not-a-uuid"), id="bad-event-id"),
    ],
)
def test_policy_server_closes_without_an_answer_to_a_malformed_request(paths, tmp_path, mutate):
    root, _ = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        request = _decision_request(discovery, root)
        mutate(request)
        raw = _policy_request(discovery["port"], **request)
    assert raw == b""


def test_policy_server_ignores_a_schema_1_request(paths, tmp_path):
    """A mixed install (older adapter, newer daemon) fails open, never denies."""
    root, _ = _same_model_setup(paths, tmp_path)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        raw = _policy_request(
            discovery["port"],
            schema_version=1,
            token=discovery["token"],
            cwd=str(root),
            session_id="session-1",
            candidate_model="opus",
        )
    assert raw == b""


def test_policy_server_bind_failure_degrades_without_crashing(paths, monkeypatch):
    """A sandboxed/job-object socket denial must degrade this optional
    capability, never take down the shared daemon Codex observation also
    depends on (ROADMAP.md: "Claude-specific failures do not break Codex")."""
    import agent_watchdog.daemon as daemon_module

    def raise_os_error(*args, **kwargs):
        raise OSError("socket denied")

    monkeypatch.setattr(daemon_module, "_PolicyServer", raise_os_error)
    socket_path = paths.data / "policy" / "socket.json"
    with _policy_server(paths):
        assert not socket_path.exists()
    assert not socket_path.exists()


def test_policy_server_publish_failure_shuts_down_the_bound_server(paths, monkeypatch):
    import agent_watchdog.daemon as daemon_module

    def raise_os_error(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(daemon_module, "atomic_write", raise_os_error)
    socket_path = paths.data / "policy" / "socket.json"
    with _policy_server(paths):
        assert not socket_path.exists()
    assert not socket_path.exists()


def test_policy_server_rejects_a_wrong_token(paths, tmp_path):
    with _policy_server(paths):
        socket_path = paths.data / "policy" / "socket.json"
        discovery = json.loads(socket_path.read_text(encoding="utf-8"))
        request = _decision_request(discovery, tmp_path)
        request["token"] = "wrong-token"
        raw = _policy_request(discovery["port"], **request)
    assert raw == b""


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("Timed out waiting for daemon")


@pytest.fixture
def paths(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    yield paths
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])


def test_concurrent_start_pause_and_explicit_resume(paths):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: start(paths), range(4)))
    wait_for(lambda: status(paths)["state"] == "running")
    instance = status(paths)["instance_id"]
    start(paths)
    assert status(paths)["instance_id"] == instance
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])
    assert status(paths)["state"] == "paused"
    assert not start(paths, explicit=False)
    assert not enqueue(
        paths, Envelope(project_id=uuid4(), provider="codex", kind="unknown", source="hook")
    )
    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")
    assert status(paths)["instance_id"] != instance


def test_registry_mutations_do_not_lose_updates(paths, tmp_path):
    roots = [tmp_path / str(index) for index in range(4)]
    for root in roots:
        root.mkdir()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda root: mutate_registry(paths, lambda registry: registry.add(root)), roots
            )
        )
    assert len(load_config(paths.config).projects) == 4


def test_daemon_drains_only_registered_events(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    event = Envelope(project_id=project.id, provider="codex", kind="unknown", source="hook")
    assert not enqueue(paths, event.model_copy(update={"project_id": uuid4()}))
    assert enqueue(paths, event)
    inbox = paths.project_data(project.id) / "inbox"
    wait_for(lambda: not list(inbox.glob("*.json")))
    stop(paths)
    wait_for(lambda: not status(paths)["alive"])
    with Store(paths.project_data(project.id), project.id) as store:
        assert [item.event_id for item in store.events()] == [event.event_id]


def test_stale_pid_is_not_treated_as_ownership(paths):
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "status.json").write_text(
        json.dumps({"pid": 1, "heartbeat": time.time(), "instance_id": "stale"})
    )
    assert status(paths)["state"] == "unavailable"
    stop(paths)
    assert status(paths)["state"] == "paused"


def test_crash_releases_lock_and_hook_recovers(paths):
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).with_name("daemon_crash_probe.py")),
            str(paths.config.parent),
        ]
    )
    try:
        wait_for(lambda: status(paths)["state"] == "running")
        instance = status(paths)["instance_id"]
        (paths.config.parent / "crash").touch()
        assert process.wait(timeout=10) == 91
        assert status(paths)["state"] == "unavailable"
        assert start(paths, explicit=False)
        wait_for(lambda: status(paths)["state"] == "running")
        assert status(paths)["instance_id"] != instance
    finally:
        if process.poll() is None:
            (paths.config.parent / "crash").touch()
            process.wait(timeout=10)


def test_stop_followed_immediately_by_start_does_not_lose_restart(paths):
    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")
    for _ in range(4):
        stop(paths)
        start(paths)
        wait_for(lambda: status(paths).get("acknowledged_request") == status(paths)["request_id"])
        assert status(paths)["alive"]


def test_bad_project_is_degraded_and_recovers(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    data = paths.project_data(project.id)
    data.mkdir(parents=True)
    database = data / "events.sqlite3"
    database.write_bytes(b"not a database")
    start(paths)
    wait_for(lambda: str(project.id) in status(paths).get("errors", []))
    assert status(paths)["state"] == "degraded"
    database.unlink()
    wait_for(lambda: status(paths)["state"] == "running")


def test_enrichment_failure_logs_are_allowlisted_debounced_and_content_free(paths):
    project_id = uuid4()
    config = Config(defaults=Limits(log_level="DEBUG"))
    logged: set[tuple[str, str]] = set()
    secret = "C:/private/rollout.jsonl prompt=private-value tool output"

    assert _record_enrichment_failures(
        paths,
        config,
        project_id,
        tuple(sorted(FAILURE_CODES)),
        logged,
        detail=secret,
    )
    assert _record_enrichment_failures(
        paths,
        config,
        project_id,
        tuple(sorted(FAILURE_CODES)),
        logged,
        detail=secret,
    )

    lines = (paths.data / "watchdog.log").read_text(encoding="ascii").splitlines()
    assert len(lines) == len(FAILURE_CODES)
    assert {line.split("error_type=", 1)[1].split()[0] for line in lines} == FAILURE_CODES
    assert all("component=daemon event=enrichment decision=unavailable" in line for line in lines)
    assert all(f"project_id={project_id}" in line for line in lines)
    assert secret not in "\n".join(lines) and "detail=" not in "\n".join(lines)

    assert not _record_enrichment_failures(paths, config, project_id, (), logged)
    assert not logged


def test_enrichment_log_detail_uses_the_global_opt_in_contract(paths):
    config = Config(defaults=Limits(log_level="DEBUG", log_detail=True))
    project_id = uuid4()
    logged: set[tuple[str, str]] = set()
    secret = "sk-proj-" + "a" * 40
    detail = f"C:/private/rollout.jsonl Bearer {secret} " + "x" * 400

    assert _record_enrichment_failures(
        paths,
        config,
        project_id,
        ("transcript_storage_unavailable",),
        logged,
        detail=detail,
    )

    line = (paths.data / "watchdog.log").read_text(encoding="ascii").strip()
    assert "error_type=transcript_storage_unavailable" in line
    assert secret not in line and ' detail="' in line and "C:/private/rollout.jsonl" in line


def source_devices(paths, project_id) -> list[bool]:
    """Per transcript source: whether a read recorded the file's identity."""
    database = paths.project_data(project_id) / "events.sqlite3"
    if not database.exists():
        return []
    # A reader beside the running daemon; opening a Store would contend for the writer.
    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)) as connection:
        try:
            rows = connection.execute("SELECT device FROM transcript_sources").fetchall()
        except sqlite3.OperationalError:
            return []
    return [device is not None for (device,) in rows]


def remove(path: Path) -> bool:
    """Delete a file the daemon may be reading; Windows refuses while it is open."""
    try:
        path.unlink()
    except PermissionError:
        return False
    return True


def test_vanished_transcript_degrades_the_daemon_until_the_source_recovers(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    transcript = tmp_path / "private-rollout.jsonl"
    meta_record = {"type": "session_meta", "payload": {"id": "session-1", "cli_version": "0.153.4"}}
    meta = json.dumps(meta_record) + "\n"
    transcript.write_text(meta, encoding="utf-8")
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    write_spool_record(
        paths,
        spool_record(
            root,
            hook_event_name="Stop",
            session_id="session-1",
            transcript_path=str(transcript),
        ),
    )

    start(paths)
    wait_for(lambda: source_devices(paths, project.id) == [True])
    wait_for(lambda: status(paths)["state"] == "running")

    # A file that never existed is only unobserved; losing one that was read is a failure.
    wait_for(lambda: remove(transcript))
    wait_for(lambda: str(project.id) in status(paths).get("errors", []))
    report = status(paths)
    assert report["state"] == "degraded"
    assert report["transcript_failures"] == {str(project.id): ["transcript_unreadable"]}
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert (
        "event=enrichment decision=unavailable "
        f"error_type=transcript_unreadable project_id={project.id}" in body
    )
    assert str(transcript) not in body

    transcript.write_text(meta, encoding="utf-8")
    wait_for(lambda: status(paths)["state"] == "running")


def test_absent_transcript_does_not_degrade_the_daemon(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    write_spool_record(
        paths,
        spool_record(
            root,
            hook_event_name="Stop",
            session_id="session-1",
            transcript_path=str(tmp_path / "not-created-yet.jsonl"),
        ),
    )

    start(paths)
    wait_for(lambda: source_devices(paths, project.id) == [False])
    wait_for(lambda: status(paths)["state"] == "running")

    report = status(paths)
    assert (report["errors"], report["transcript_failures"]) == ([], {})


def test_stale_transcript_failure_does_not_degrade_daemon(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    # A directory is a failure that cannot heal; an absent file is only unobserved.
    not_a_file = tmp_path / "not-a-file.jsonl"
    not_a_file.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    updated = config.model_copy(
        update={
            "projects": (
                project.model_copy(update={"overrides": Overrides(transcript_failure_minutes=1)}),
            )
        }
    )
    save_config(paths.config, updated)
    record = spool_record(
        root,
        hook_event_name="Stop",
        session_id="session-1",
        transcript_path=str(not_a_file),
    )
    record["received_at"] = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
    write_spool_record(paths, record)

    start(paths)
    wait_for(lambda: status(paths)["state"] == "running")

    with Store(paths.project_data(project.id), project.id) as store:
        sources = store.transcript_sources()
    assert sources[0]["last_error"] == "transcript_path_not_regular"
    assert status(paths)["transcript_failures"] == {}


def test_inert_enrichment_is_logged_and_degrades_until_it_resolves(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    transcript = tmp_path / "session.jsonl"
    skipped = {
        "type": "assistant",
        "sessionId": "session-1",
        "requestId": "req_child",
        "isSidechain": True,
        "timestamp": "2026-09-09T04:58:34.955Z",
        "version": "2.1.260",
        "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 1, "output_tokens": 9}},
    }
    transcript.write_text(json.dumps(skipped) + "\n", encoding="utf-8")
    mutate_registry(paths, lambda registry: registry.add(root))
    project = load_config(paths.config).projects[0]
    write_spool_record(
        paths,
        spool_record(
            root,
            hook_event_name="Stop",
            session_id="session-1",
            transcript_path=str(transcript),
        )
        | {"provider": "claude"},
    )

    start(paths)
    wait_for(lambda: str(project.id) in status(paths).get("errors", []))
    assert status(paths)["state"] == "degraded"
    body = (paths.data / "watchdog.log").read_text(encoding="ascii")
    assert (
        "event=enrichment decision=inert reason=usage_seen_unstored "
        f"project_id={project.id}" in body
    )
    assert str(transcript) not in body
    assert body.count("decision=inert") == 1  # debounced

    # An accepted (non-sidechain) line clears the inert state.
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(skipped | {"requestId": "req_real", "isSidechain": False}) + "\n")
    wait_for(lambda: str(project.id) not in status(paths).get("errors", []))


@pytest.mark.parametrize("content", ['{"schema_version": 99}', "{}"])
def test_corrupt_control_is_never_overwritten(paths, content):
    paths.data.mkdir(parents=True)
    control = paths.data / "control.json"
    control.write_text(content)
    with pytest.raises(ValueError):
        start(paths)
    with pytest.raises(ValueError):
        stop(paths)
    assert control.read_text() == content
    control.unlink()


def test_stale_heartbeat_under_a_held_lock_is_degraded(paths):
    paths.data.mkdir(parents=True)
    paths.runtime.mkdir(parents=True)
    (paths.runtime / "status.json").write_text(json.dumps({"heartbeat": 1, "pid": 1}))
    with writer_lock(paths.data / "daemon.lock"):
        assert status(paths)["state"] == "degraded"
        assert status(paths)["alive"]
    assert status(paths)["state"] == "unavailable"


def test_status_reports_current_queue_occupancy(paths, tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    config = load_config(paths.config)
    project = config.projects[0]
    Inbox(paths.project_data(project.id)).publish(
        Envelope(project_id=project.id, provider="codex", kind="unknown", source="hook")
    )

    queues = status(paths)["queues"]
    assert queues["spool"]["files"] == 0
    inbox = queues["projects"][str(project.id)]
    assert inbox["files"] == 1 and inbox["bytes"] > 0
    assert inbox["byte_limit"] == config.defaults.inbox_bytes
    assert inbox["file_limit"] is None


def test_hook_start_contention_is_bounded(paths):
    paths.data.mkdir(parents=True)
    with writer_lock(paths.data / "control.lock"):
        before = time.monotonic()
        with pytest.raises(WriterBusy):
            start(paths, explicit=False)
        assert time.monotonic() - before < 1


@pytest.mark.skipif(os.name != "nt", reason="Windows process creation flags are platform-specific")
def test_daemon_launch_uses_an_invisible_process_group(paths, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows process creation flags are platform-specific")
    save_config(paths.config, Config())
    calls = []

    class FakeProcess:
        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        calls.append(kwargs)
        return FakeProcess()

    monkeypatch.setattr("agent_watchdog.daemon.subprocess.Popen", fake_popen)
    launch(paths)

    flags = calls[0]["creationflags"]
    assert flags & subprocess.CREATE_NO_WINDOW
    assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    assert not flags & subprocess.DETACHED_PROCESS


def test_independent_cli_startup_contenders_and_pause(paths):
    command = [sys.executable, "-m", "agent_watchdog", "--home", str(paths.config.parent), "daemon"]
    processes = [
        subprocess.Popen(
            command + ["start"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(3)
    ]
    try:
        reports = []
        for process in processes:
            output, errors = process.communicate(timeout=15)
            assert process.returncode == 0, (output, errors)
            reports.append(json.loads(output))
        assert len({report["instance_id"] for report in reports}) == 1
        result = subprocess.run(command + ["pause"], capture_output=True, text=True, timeout=15)
        assert result.returncode == 0
        assert json.loads(result.stdout)["alive"] is False
    finally:
        stop(paths)
        for process in processes:
            process.communicate(timeout=15)
