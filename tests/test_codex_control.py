"""WD-151: installation-scoped Codex control through approved rules."""

import hashlib
import json
import socket
import tomllib
from uuid import uuid4

import pytest
import tomli_w

from agent_watchdog.config import Rules, UserPaths, load_config, save_config
from agent_watchdog.daemon import _policy_server, _SessionTracker, mutate_registry
from agent_watchdog.rules.api import renderable
from agent_watchdog.rules.declarative import RuleError, parse_rule
from agent_watchdog.rules.engine import decide, subscriptions

OLD = "windows-cli-0.160.0"
EXEC = "windows-cli-exec-0.162.0-alpha.17.2"
INTERACTIVE = "windows-cli-interactive-0.162.0-alpha.17.2"
DESKTOP = "windows-desktop-26.1016.62342-0.163.0-alpha.11.2"

DESKTOP_CELLS = [
    (DESKTOP, "SessionStart", None, "context"),
    (DESKTOP, "PreToolUse", "Bash", "deny"),
    (DESKTOP, "PreToolUse", "Bash", "rewrite"),
    (DESKTOP, "PreToolUse", "apply_patch", "deny"),
    (DESKTOP, "PreToolUse", "apply_patch", "rewrite"),
    (DESKTOP, "PermissionRequest", "Bash", "deny"),
    (DESKTOP, "PermissionRequest", "Bash", "permit"),
    (DESKTOP, "PostToolUse", "Bash", "block"),
    (DESKTOP, "Stop", None, "block"),
    (DESKTOP, "SubagentStop", None, "block"),
]

CELLS = [
    (OLD, "SessionStart", None, "context"),
    (OLD, "UserPromptSubmit", None, "context"),
    (OLD, "PostToolUse", "Bash", "context"),
    (OLD, "PreToolUse", "Bash", "deny"),
    (OLD, "PreToolUse", "Bash", "rewrite"),
    (OLD, "SubagentStop", None, "block"),
    (EXEC, "PreToolUse", "Bash", "context"),
    (EXEC, "PreToolUse", "apply_patch", "context"),
    (EXEC, "PreToolUse", "apply_patch", "deny"),
    (EXEC, "SubagentStart", None, "context"),
    (EXEC, "Stop", None, "block"),
    (INTERACTIVE, "PreToolUse", "apply_patch", "rewrite"),
    *DESKTOP_CELLS,
]


@pytest.fixture
def installation(tmp_path, monkeypatch):
    # Exercise the versioned Windows contract on every CI OS; a separate test
    # below proves that a real non-Windows deployment is gated.
    monkeypatch.setattr("agent_watchdog.rules.api._WINDOWS", True, raising=False)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    root = tmp_path / "project"
    root.mkdir()
    mutate_registry(paths, lambda registry: registry.add(root))
    return paths, root


def approve(paths, event, tool, action, *, mixed=False):
    path = paths.config.parent / "rules" / "codex_guard.toml"
    path.parent.mkdir(exist_ok=True)
    body = {
        "schema_version": 1,
        "name": "codex_guard",
        "version": "1",
        "origin": "user",
        "provider": ["codex", "claude"] if mixed else ["codex"],
        "event": event,
        "match": {} if tool is None else {"tool_name": tool},
        "action": {"kind": action, "message": "Watchdog: synthetic control check."},
    }
    if action == "rewrite":
        body["action"]["rewrite"] = {"tool_input.command": "echo rewritten"}
    path.write_text(tomli_w.dumps(body), encoding="utf-8")
    config = load_config(paths.config)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    save_config(
        paths.config, config.model_copy(update={"rules": Rules(approved={"codex_guard": digest})})
    )
    return path


def hook(root, event, tool):
    value = {"cwd": str(root), "session_id": "synthetic-session", "hook_event_name": event}
    if tool is not None:
        value.update(tool_name=tool, tool_input={"command": "echo original", "other": "preserved"})
    return value


@pytest.mark.parametrize("profile,event,tool,action", CELLS)
def test_approved_rules_reach_every_scoped_cell(installation, profile, event, tool, action):
    paths, root = installation
    approve(paths, event, tool, action)
    config = load_config(paths.config)
    result = decide(paths, config, "codex", hook(root, event, tool), codex_control_profile=profile)
    assert result.action == action
    assert result.project_id == config.projects[0].id
    if action == "rewrite":
        assert result.updated_input == {"command": "echo rewritten", "other": "preserved"}
    assert {"provider": "codex", "hook_event_name": event, "tool_name": tool} in subscriptions(
        paths, config
    )


@pytest.mark.parametrize(
    "profile", [None, "unknown", "windows-cli-0.161.0", "desktop-0.160.0", {}, EXEC]
)
def test_payload_attribution_does_not_enable_cli_permissions(installation, profile):
    paths, root = installation
    approve(paths, "PreToolUse", "Bash", "deny")
    payload = {
        **hook(root, "PreToolUse", "Bash"),
        "surface": "cli",
        "provider_version": "0.160.0",
        "codex_control_profile": OLD,
    }
    result = decide(
        paths, load_config(paths.config), "codex", payload, codex_control_profile=profile
    )
    assert result.action == "log"
    assert result.capability_gap == "unsupported_codex_cell"
    assert result.requested_action == "deny"
    # A repeated gap is bounded; it must not consume the real action's throttle.
    assert (
        decide(
            paths, load_config(paths.config), "codex", payload, codex_control_profile=profile
        ).action
        == "allow"
    )
    assert (
        decide(paths, load_config(paths.config), "codex", payload, codex_control_profile=OLD).action
        == "deny"
    )


@pytest.mark.parametrize(
    "event,tool,action,profile",
    [
        ("PreToolUse", "Agent", "deny", OLD),
        ("PreToolUse", "Bash", "ask", OLD),
        ("PostToolUse", "Bash", "block", OLD),
        ("PreCompact", None, "context", EXEC),
        ("PreToolUse", "apply_patch", "rewrite", EXEC),
        ("PreToolUse", "mcp__other__emit", "deny", EXEC),
    ],
)
def test_unsupported_cells_are_log_only(installation, event, tool, action, profile):
    paths, root = installation
    # Mixed-provider rules can ask for a Claude-only action; Codex must record
    # the gap without inheriting that provider's permissions.
    approve(paths, event, tool, action, mixed=True)
    result = decide(
        paths,
        load_config(paths.config),
        "codex",
        hook(root, event, tool),
        codex_control_profile=profile,
    )
    assert result.action == "log"
    assert result.capability_gap == "unsupported_codex_cell"


@pytest.mark.parametrize(
    "event,tool,action",
    [
        # Probed unsupported or inconclusive on the desktop build (WD-153).
        ("UserPromptSubmit", None, "context"),
        ("PreToolUse", "Bash", "context"),
        ("PreToolUse", "apply_patch", "context"),
        ("PostToolUse", "Bash", "context"),
        ("PreToolUse", "Bash", "ask"),
        ("SubagentStart", None, "context"),
        ("PreCompact", None, "context"),
        # Supported event and action, but never probed for this tool.
        ("PreToolUse", "mcp__other__emit", "deny"),
        ("PostToolUse", "apply_patch", "block"),
    ],
)
def test_desktop_profile_keeps_unsupported_cells_log_only(installation, event, tool, action):
    paths, root = installation
    approve(paths, event, tool, action, mixed=True)
    result = decide(
        paths,
        load_config(paths.config),
        "codex",
        hook(root, event, tool),
        codex_control_profile=DESKTOP,
    )
    assert result.action == "log"
    assert result.capability_gap == "unsupported_codex_cell"
    assert not renderable("codex", event, action, tool=tool, codex_control_profile=DESKTOP)


@pytest.mark.parametrize(
    "tool,action",
    [
        ("Bash", "rewrite"),
        ("Bash", "ask"),
        ("Bash", "context"),
        ("apply_patch", "deny"),
        ("apply_patch", "permit"),
    ],
)
def test_unprobed_permission_cells_cannot_be_promised_by_a_rule(installation, tool, action):
    paths, _ = installation
    path = approve(paths, "PermissionRequest", tool, action)
    with pytest.raises(RuleError, match="cannot be delivered"):
        parse_rule(path.read_text())


@pytest.mark.parametrize(
    "profile,event,tool,action",
    [
        # The desktop gate must not leak into CLI profiles, nor the reverse.
        (OLD, "PreToolUse", "apply_patch", "deny"),
        (OLD, "Stop", None, "block"),
        (OLD, "PostToolUse", "Bash", "block"),
        (OLD, "PermissionRequest", "Bash", "deny"),
        (EXEC, "PermissionRequest", "Bash", "permit"),
        (INTERACTIVE, "PermissionRequest", "Bash", "permit"),
        # `permit` auto-approves; no event but the permission prompt may carry it.
        (DESKTOP, "PreToolUse", "Bash", "permit"),
        (DESKTOP, "PostToolUse", "Bash", "permit"),
        (EXEC, "SessionStart", None, "context"),
        (INTERACTIVE, "PreToolUse", "Bash", "deny"),
        (DESKTOP, "UserPromptSubmit", None, "context"),
        (DESKTOP, "SubagentStart", None, "context"),
        ("windows-desktop-26.1016.62342", "PreToolUse", "Bash", "deny"),
        ("windows-desktop-26.1016.62343-0.163.0-alpha.11.2", "PreToolUse", "Bash", "deny"),
        ("windows-desktop-26.1016.62342-0.163.0-alpha.11.3", "PreToolUse", "Bash", "deny"),
    ],
)
def test_profiles_do_not_inherit_each_others_cells(profile, event, tool, action):
    assert not renderable("codex", event, action, tool=tool, codex_control_profile=profile)


@pytest.mark.parametrize("surface,version", [("desktop", "26.1016.62342"), ("cli", "0.163.0")])
def test_payload_surface_cannot_select_the_desktop_profile(installation, surface, version):
    paths, root = installation
    approve(paths, "PreToolUse", "Bash", "deny")
    payload = {
        **hook(root, "PreToolUse", "Bash"),
        "surface": surface,
        "provider_version": version,
        "codex_control_profile": DESKTOP,
    }
    result = decide(paths, load_config(paths.config), "codex", payload)
    assert result.action == "log"
    assert result.capability_gap == "unsupported_codex_cell"


@pytest.mark.parametrize(
    "event,tool,providers,match",
    [
        ("PermissionRequest", None, ["codex"], "cannot be delivered|names? .*tool"),
        ("PreToolUse", "Bash", ["codex"], "cannot be delivered"),
        ("PermissionRequest", "Bash", ["claude"], "cannot be delivered"),
    ],
)
def test_a_permit_rule_needs_an_explicit_codex_permission_tool(
    installation, event, tool, providers, match
):
    paths, _ = installation
    path = approve(paths, event, tool, "permit")
    body = tomllib.loads(path.read_text())
    body["provider"] = providers
    with pytest.raises(RuleError, match=match):
        parse_rule(tomli_w.dumps(body))


@pytest.mark.parametrize("switch", ["global", "project", "unapproved", "wrong-profile"])
def test_permit_respects_every_guard(installation, switch):
    paths, root = installation
    approve(paths, "PermissionRequest", "Bash", "permit")
    config = load_config(paths.config)
    profile = DESKTOP
    if switch == "global":
        config = config.model_copy(
            update={"defaults": config.defaults.model_copy(update={"policy_intervene": False})}
        )
    elif switch == "project":
        project = config.projects[0]
        overrides = project.overrides.model_copy(update={"policy_intervene": False})
        config = config.model_copy(
            update={"projects": [project.model_copy(update={"overrides": overrides})]}
        )
    elif switch == "unapproved":
        config = config.model_copy(update={"rules": Rules()})
    else:
        profile = OLD
    result = decide(
        paths,
        config,
        "codex",
        hook(root, "PermissionRequest", "Bash"),
        codex_control_profile=profile,
    )
    assert result.action in {"allow", "log"}
    assert result.action != "permit"


def test_permit_is_recorded_as_a_control_finding(installation):
    paths, root = installation
    approve(paths, "PermissionRequest", "Bash", "permit")
    payload = {**hook(root, "PermissionRequest", "Bash"), "codex_control_profile": OLD}
    with _policy_server(paths, _SessionTracker()):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        assert request(discovery, payload, DESKTOP)["action"] == "permit"
    records = [
        json.loads(p.read_text())["envelope"]["payload"]["codex"]
        for p in (paths.data / "spool").glob("*.json")
        if p.name != "limits.json"
    ]
    assert any(
        r["action"] == "permit" and r["rule"] == "codex_guard" and r["delivered_at"] and r["text"]
        for r in records
    )


def test_non_windows_deployment_is_unavailable(monkeypatch):
    monkeypatch.setattr("agent_watchdog.rules.api._WINDOWS", False, raising=False)
    assert not renderable("codex", "PreToolUse", "deny", tool="Bash", codex_control_profile=OLD)


def test_wildcard_subscription_is_narrowed_and_changed_rule_loses_approval(installation):
    paths, root = installation
    path = approve(paths, "PreToolUse", None, "deny")
    config = load_config(paths.config)
    codex = [row for row in subscriptions(paths, config) if row["provider"] == "codex"]
    assert codex == [
        {"provider": "codex", "hook_event_name": "PreToolUse", "tool_name": "Bash"},
        {"provider": "codex", "hook_event_name": "PreToolUse", "tool_name": "apply_patch"},
    ]
    payload = hook(root, "PreToolUse", "Bash")
    assert decide(paths, config, "codex", payload, codex_control_profile=OLD).action == "deny"
    path.write_text(path.read_text().replace("synthetic control check", "changed control check"))
    assert decide(paths, config, "codex", payload, codex_control_profile=OLD).action == "allow"


def test_a_codex_only_rule_cannot_promise_an_unprobed_tool(installation):
    paths, _ = installation
    path = approve(paths, "PreToolUse", "Agent", "deny")
    with pytest.raises(RuleError, match="cannot be delivered"):
        parse_rule(path.read_text())


def test_gap_dedup_expires(installation, monkeypatch):
    paths, root = installation
    approve(paths, "PreToolUse", "Bash", "deny")
    payload = hook(root, "PreToolUse", "Bash")
    clock = [100.0]
    monkeypatch.setattr("agent_watchdog.rules.engine.time.monotonic", lambda: clock[0])
    config = load_config(paths.config)
    assert decide(paths, config, "codex", payload).capability_gap == "unsupported_codex_cell"
    clock[0] += 599
    assert decide(paths, config, "codex", payload).action == "allow"
    clock[0] += 1
    assert decide(paths, config, "codex", payload).capability_gap == "unsupported_codex_cell"


def test_gap_does_not_mask_a_later_supported_rule(installation):
    paths, root = installation
    path = approve(paths, "PreToolUse", "Bash", "deny", mixed=True)
    first = path.with_name("earlier_ask.toml")
    body = tomllib.loads(path.read_text())
    body.update(name="earlier_ask", priority=1)
    body["action"]["kind"] = "ask"
    first.write_text(tomli_w.dumps(body))
    config = load_config(paths.config)
    approved = {
        **config.rules.approved,
        "earlier_ask": hashlib.sha256(first.read_bytes()).hexdigest(),
    }
    config = config.model_copy(update={"rules": Rules(approved=approved)})
    result = decide(
        paths, config, "codex", hook(root, "PreToolUse", "Bash"), codex_control_profile=OLD
    )
    assert result.action == "deny"
    assert result.rule == "codex_guard"


@pytest.mark.parametrize("active", [True, None, "false", 0, {}, []])
def test_malformed_or_active_stop_flag_cannot_be_blocked(installation, active):
    paths, root = installation
    approve(paths, "Stop", None, "block")
    payload = {**hook(root, "Stop", None), "stop_hook_active": active}
    result = decide(paths, load_config(paths.config), "codex", payload, codex_control_profile=EXEC)
    assert result.action == "allow"


@pytest.mark.parametrize("tool_input", [None, {}, {"command": {}}, {"command": 1}])
def test_rewrite_needs_an_original_string_command(installation, tool_input):
    paths, root = installation
    approve(paths, "PreToolUse", "Bash", "rewrite")
    payload = {**hook(root, "PreToolUse", "Bash"), "tool_input": tool_input}
    result = decide(paths, load_config(paths.config), "codex", payload, codex_control_profile=OLD)
    assert result.action in {"allow", "log"}
    if result.action == "log":
        assert result.capability_gap == "unsupported_codex_cell"


@pytest.mark.parametrize("switch", ["global", "project", "stop_hook_active", "unapproved"])
def test_control_guards(installation, switch):
    paths, root = installation
    approve(paths, "Stop", None, "block")
    config = load_config(paths.config)
    payload = hook(root, "Stop", None)
    if switch == "global":
        config = config.model_copy(
            update={"defaults": config.defaults.model_copy(update={"policy_intervene": False})}
        )
    elif switch == "project":
        project = config.projects[0]
        overrides = project.overrides.model_copy(update={"policy_intervene": False})
        config = config.model_copy(
            update={"projects": [project.model_copy(update={"overrides": overrides})]}
        )
    elif switch == "stop_hook_active":
        payload["stop_hook_active"] = True
    else:
        config = config.model_copy(update={"rules": Rules()})
    assert decide(paths, config, "codex", payload, codex_control_profile=EXEC).action == "allow"


def request(discovery, payload, profile):
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


def test_daemon_reads_profile_only_from_request_and_records_gap(installation):
    paths, root = installation
    approve(paths, "PreToolUse", "Bash", "deny")
    payload = {**hook(root, "PreToolUse", "Bash"), "codex_control_profile": OLD}
    with _policy_server(paths, _SessionTracker()):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_text())
        assert request(discovery, payload, None)["action"] == "allow"
        assert request(discovery, payload, OLD)["action"] == "deny"
    records = [
        json.loads(p.read_text())["envelope"]["payload"]["codex"]
        for p in (paths.data / "spool").glob("*.json")
        if p.name != "limits.json"
    ]
    assert any(
        r.get("capability_gap") == "unsupported_codex_cell"
        and r["action"] == "log"
        and r["delivered_at"] is None
        for r in records
    )
    assert any(r["action"] == "deny" and r["delivered_at"] for r in records)
