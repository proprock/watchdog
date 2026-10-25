"""WD-180: provisional Codex profile, deny effect confirmation and demotion."""

import hashlib
import json
import socket
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import tomli_w

from agent_watchdog import demotion
from agent_watchdog.analysis import control_findings
from agent_watchdog.config import Rules, UserPaths, load_config, save_config
from agent_watchdog.daemon import _drain_spool, _policy_server, _SessionTracker, mutate_registry
from agent_watchdog.events import Envelope
from agent_watchdog.rules.api import CODEX_CELLS, PROVISIONAL_PROFILES
from agent_watchdog.rules.engine import decide

DESKTOP = "windows-desktop-26.1016.62342-0.163.0-alpha.11.2"
PROVISIONAL = "windows-desktop-26.1021.31345-0.164.0-alpha.11.1"
SESSION = "provisional-session"


@pytest.fixture(autouse=True)
def clean_demotion_state():
    demotion.reset()
    yield
    demotion.reset()


@pytest.fixture
def installation(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_watchdog.rules.api._WINDOWS", True, raising=False)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    return paths, root


def approve(paths, event, tool, action):
    path = paths.config.parent / "rules" / "codex_guard.toml"
    path.parent.mkdir(exist_ok=True)
    body = {
        "schema_version": 1,
        "name": "codex_guard",
        "version": "1",
        "origin": "user",
        "provider": ["codex"],
        "event": event,
        "match": {} if tool is None else {"tool_name": tool},
        "action": {"kind": action, "message": "Watchdog: synthetic control check."},
    }
    if action == "rewrite":
        body["action"]["rewrite"] = {"tool_input.command": "echo rewritten"}
    if action == "permit":
        body["match"] = {**body["match"], "input_regex": "echo original"}
    path.write_text(tomli_w.dumps(body), encoding="utf-8")
    config = load_config(paths.config)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    save_config(
        paths.config, config.model_copy(update={"rules": Rules(approved={"codex_guard": digest})})
    )


def hook(root, event, tool, tool_use_id="call-1"):
    value = {"cwd": str(root), "session_id": SESSION, "hook_event_name": event}
    if tool is not None:
        value.update(
            tool_name=tool,
            tool_use_id=tool_use_id,
            tool_input={"command": "echo original", "other": "preserved"},
        )
    return value


def test_provisional_profile_carries_only_low_risk_actions():
    assert PROVISIONAL in CODEX_CELLS and PROVISIONAL in PROVISIONAL_PROFILES
    assert DESKTOP not in PROVISIONAL_PROFILES
    cells = set(CODEX_CELLS[PROVISIONAL])
    assert cells and cells <= set(CODEX_CELLS[DESKTOP])
    assert {action for _, _, action in cells} == {"deny", "block", "context"}


@pytest.mark.parametrize("event,tool,action", sorted(CODEX_CELLS[PROVISIONAL], key=str))
def test_provisional_profile_delivers_its_cells(installation, event, tool, action):
    paths, root = installation
    approve(paths, event, tool, action)
    config = load_config(paths.config)
    result = decide(
        paths, config, "codex", hook(root, event, tool), codex_control_profile=PROVISIONAL
    )
    assert result.action == action
    assert result.codex_control_profile == PROVISIONAL


@pytest.mark.parametrize(
    "event,tool,action",
    [
        ("PreToolUse", "Bash", "rewrite"),
        ("PreToolUse", "apply_patch", "rewrite"),
        ("PermissionRequest", "Bash", "permit"),
    ],
)
def test_actions_that_change_or_approve_a_call_stay_log_only(installation, event, tool, action):
    paths, root = installation
    approve(paths, event, tool, action)
    config = load_config(paths.config)
    result = decide(
        paths, config, "codex", hook(root, event, tool), codex_control_profile=PROVISIONAL
    )
    assert result.action == "log"
    assert result.capability_gap == "unsupported_codex_cell"
    assert result.requested_action == action


def test_a_demoted_profile_logs_instead_of_acting_and_leaves_others_alone(installation):
    paths, root = installation
    approve(paths, "PreToolUse", "Bash", "deny")
    config = load_config(paths.config)
    payload = hook(root, "PreToolUse", "Bash")
    demotion.demote(paths, PROVISIONAL, evidence="call-9")
    demoted = decide(paths, config, "codex", payload, codex_control_profile=PROVISIONAL)
    assert demoted.action == "log"
    assert demoted.requested_action == "deny"
    assert demoted.capability_gap == "profile_demoted"
    # The exact, probed profile is not a provisional one and is never demoted.
    assert decide(paths, config, "codex", payload, codex_control_profile=DESKTOP).action == "deny"
    demotion.demote(paths, DESKTOP, evidence="call-9")
    assert decide(paths, config, "codex", payload, codex_control_profile=DESKTOP).action == "deny"


def finish(
    session=SESSION, tool_use_id="call-1", *, provider="codex", kind="tool.finish", content=None
):
    namespace = {"tool_use_id": tool_use_id}
    if content is not None:
        namespace["content"] = content
    return Envelope(
        provider=provider,
        project_id=uuid4(),
        session_id=session,
        kind=kind,
        source="hook",
        payload={provider: namespace},
    )


@pytest.mark.parametrize(
    "content,demoted",
    [
        # The harness echoing the daemon's own refusal is not an execution.
        ({"tool_response": "Command blocked by PreToolUse hook: WD151_DENY."}, False),
        ({"tool_response": {"output": "x", "error": "blocked: WD151_DENY"}}, False),
        ({"tool_response": "original output"}, True),
        ("omitted", True),  # content capture off: nothing shows it was a refusal
        ({}, True),
    ],
)
def test_a_finish_that_only_echoes_the_denial_does_not_demote(installation, content, demoted):
    paths, _ = installation
    demotion.remember_denial(SESSION, "call-1", PROVISIONAL, "WD151_DENY")
    result = demotion.observe(paths, finish(content=content))
    assert (result == PROVISIONAL) is demoted
    assert demotion.is_demoted(paths, PROVISIONAL) is demoted


def test_only_a_remembered_denial_is_contradicted_by_a_finished_call(installation):
    paths, _ = installation
    demotion.remember_denial(SESSION, "call-1", PROVISIONAL, None)
    for other in (
        finish(tool_use_id="call-2"),
        finish(session="other"),
        finish(provider="claude"),
        finish(kind="tool.start"),
    ):
        assert demotion.observe(paths, other) is None
    assert not demotion.is_demoted(paths, PROVISIONAL)
    assert demotion.observe(paths, finish()) == PROVISIONAL
    assert demotion.is_demoted(paths, PROVISIONAL)


def test_demotion_survives_a_restart_and_a_damaged_record_counts_as_demoted(installation):
    paths, _ = installation
    demotion.demote(paths, PROVISIONAL, evidence="call-1")
    demotion.reset()
    assert demotion.is_demoted(paths, PROVISIONAL)
    assert not demotion.is_demoted(paths, DESKTOP)
    (paths.data / "profile-demotions.json").write_text("{not json", encoding="utf-8")
    demotion.reset()
    assert demotion.is_demoted(paths, PROVISIONAL)


def ask(discovery, payload, profile):
    document = {
        "schema_version": 2,
        "token": discovery["token"],
        "provider": "codex",
        "event_id": str(uuid4()),
        "input": payload,
        "codex_control_profile": profile,
    }
    with socket.create_connection(("127.0.0.1", discovery["port"]), timeout=5) as client:
        client.sendall((json.dumps(document) + "\n").encode())
        client.shutdown(socket.SHUT_WR)
        output = b""
        while chunk := client.recv(65536):
            output += chunk
    return json.loads(output)


def spooled(paths, record):
    directory = paths.data / "spool"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{uuid4().hex}.json").write_text(json.dumps(record), encoding="utf-8")


def post_tool_use(root, tool_use_id):
    return {
        "schema_version": 2,
        "record_type": "hook",
        "event_id": str(uuid4()),
        "received_at": datetime.now(UTC).isoformat(),
        "provider": "codex",
        "cwd": str(root),
        "input": {
            "hook_event_name": "PostToolUse",
            "session_id": SESSION,
            "tool_name": "Bash",
            "tool_use_id": tool_use_id,
            "tool_input": {"command": "echo original"},
            "tool_response": "original",
        },
    }


def test_a_denied_call_that_still_finishes_demotes_the_provisional_profile(installation):
    paths, root = installation
    approve(paths, "PreToolUse", "Bash", "deny")
    config = load_config(paths.config)
    with _policy_server(paths, _SessionTracker()):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        assert ask(discovery, hook(root, "PreToolUse", "Bash"), PROVISIONAL)["action"] == "deny"
        # A different call finishing is no evidence against the denial.
        spooled(paths, post_tool_use(root, "call-2"))
        assert _drain_spool(paths, config)
        assert not demotion.is_demoted(paths, PROVISIONAL)
        spooled(paths, post_tool_use(root, "call-1"))
        assert _drain_spool(paths, config)
        assert demotion.is_demoted(paths, PROVISIONAL)
        assert ask(discovery, hook(root, "PreToolUse", "Bash"), PROVISIONAL)["action"] == "allow"
        # The probed profile keeps acting.
        assert ask(discovery, hook(root, "PreToolUse", "Bash"), DESKTOP)["action"] == "deny"


def test_the_probed_profile_is_never_tracked_for_demotion(installation):
    paths, root = installation
    approve(paths, "PreToolUse", "Bash", "deny")
    config = load_config(paths.config)
    with _policy_server(paths, _SessionTracker()):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        assert ask(discovery, hook(root, "PreToolUse", "Bash"), DESKTOP)["action"] == "deny"
    spooled(paths, post_tool_use(root, "call-1"))
    assert _drain_spool(paths, config)
    assert not demotion.is_demoted(paths, DESKTOP)


MOMENT = datetime(2026, 10, 25, tzinfo=UTC)


def codex_hook(project, event_id, moment, *, kind="tool.start", session=SESSION, tool_use_id="c1"):
    payload = {} if tool_use_id is None else {"tool_use_id": tool_use_id}
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=session,
        kind=kind,
        source="hook",
        received_at=moment,
        event_id=event_id,
        payload={"codex": payload},
    )


def codex_control(project, hook_id, moment, *, action="deny", **fields):
    recorded = {
        "rule": "guard",
        "rule_version": "1",
        "action": action,
        "text": "Watchdog: denied.",
        "evidence_ids": [str(hook_id)],
        "delivered_at": moment.isoformat(),
    } | fields
    return Envelope(
        provider="codex",
        project_id=project,
        session_id=SESSION,
        kind="control",
        source="daemon",
        received_at=moment,
        payload={"codex": recorded},
    )


def effect(*events):
    (finding,) = control_findings(list(events))
    return finding["provider_effect"]


def test_a_denial_is_confirmed_only_by_later_activity_without_the_call_finishing():
    project, hook_id = uuid4(), uuid4()
    start = codex_hook(project, hook_id, MOMENT)
    control = codex_control(project, hook_id, MOMENT + timedelta(seconds=1))
    later = codex_hook(
        project, uuid4(), MOMENT + timedelta(seconds=5), kind="turn.end", tool_use_id=None
    )
    assert effect(start, control) is None  # nothing after the denial: unknown, not blocked
    assert effect(start, control, later) == "blocked"


def test_a_call_that_finishes_after_its_denial_contradicts_it():
    project, hook_id = uuid4(), uuid4()
    start = codex_hook(project, hook_id, MOMENT)
    control = codex_control(project, hook_id, MOMENT + timedelta(seconds=1))
    done = codex_hook(project, uuid4(), MOMENT + timedelta(seconds=3), kind="tool.finish")
    later = codex_hook(
        project, uuid4(), MOMENT + timedelta(seconds=5), kind="turn.end", tool_use_id=None
    )
    assert effect(start, control, done) == "contradicted"
    assert effect(start, control, done, later) == "contradicted"


@pytest.mark.parametrize(
    "changes",
    [
        {"session": "other"},  # activity in another session proves nothing
    ],
)
def test_effect_ignores_activity_that_cannot_belong_to_the_denied_call(changes):
    project, hook_id = uuid4(), uuid4()
    start = codex_hook(project, hook_id, MOMENT)
    control = codex_control(project, hook_id, MOMENT + timedelta(seconds=1))
    unrelated = codex_hook(
        project,
        uuid4(),
        MOMENT + timedelta(seconds=5),
        kind="turn.end",
        tool_use_id=None,
        **changes,
    )
    assert effect(start, control, unrelated) is None


def test_effect_needs_a_codex_deny_with_an_observed_call_identity():
    project, hook_id = uuid4(), uuid4()
    later = codex_hook(
        project, uuid4(), MOMENT + timedelta(seconds=5), kind="turn.end", tool_use_id=None
    )
    anonymous = codex_hook(project, hook_id, MOMENT, tool_use_id=None)
    control = codex_control(project, hook_id, MOMENT + timedelta(seconds=1))
    assert effect(anonymous, control, later) is None
    start = codex_hook(project, hook_id, MOMENT)
    context = codex_control(project, hook_id, MOMENT + timedelta(seconds=1), action="context")
    assert effect(start, context, later) is None
    # A value the daemon recorded itself is never trusted as an observed effect.
    forged = codex_control(
        project, hook_id, MOMENT + timedelta(seconds=1), provider_effect="blocked"
    )
    assert effect(start, forged) is None
