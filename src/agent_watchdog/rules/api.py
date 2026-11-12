"""Decision vocabulary shared by the daemon's decision channel and its rules."""

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel

from agent_watchdog.config import Config, UserPaths
from agent_watchdog.state import SessionState

# ``log`` records a finding and sends nothing: the adapter is told ``allow``.
#
# ``permit`` approves a pending permission prompt and so bypasses the user's own
# approval. It differs from ``allow`` (no opinion) and is deliverable only for a
# Codex ``PermissionRequest`` on an exact profile (see ``CODEX_CELLS``).
Action = Literal["allow", "log", "deny", "ask", "rewrite", "context", "block", "permit"]

ACTIONS: frozenset[str] = frozenset(
    {"allow", "log", "deny", "ask", "rewrite", "context", "block", "permit"}
)

# Event values are recursively frozen before being handed to Python rules.
Event = Mapping[str, object]


def _readonly(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _readonly(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_readonly(item) for item in value)
    if isinstance(value, BaseModel):
        return value.model_copy(
            update={name: _readonly(getattr(value, name)) for name in type(value).model_fields}
        )
    return value


# Keep the capability contracts in step with `render` in native/src/main.rs.
# These are provider-probe cells, not production-adapter live acceptance.
_RENDERABLE: Mapping[str, Mapping[str, frozenset[str]]] = {
    "claude": {
        "PreToolUse": frozenset({"deny", "ask", "rewrite", "context"}),
        "PostToolUse": frozenset({"block", "context"}),
        "UserPromptSubmit": frozenset({"context"}),
        "Stop": frozenset({"block"}),
        "SubagentStop": frozenset({"block"}),
        "SessionStart": frozenset({"context"}),
        # `PreCompact` block is only weakly evidenced (WD-139: no effect seen),
        # so only its context cell is rendered.
        "PreCompact": frozenset({"context"}),
    }
}

_WINDOWS = sys.platform == "win32"
# Exact deployment profiles are explicitly selected by the installation command,
# never inferred from hook payloads, PATH, or another surface's installed CLI.
# A None tool means the event itself is scoped, rather than an arbitrary tool.
_DESKTOP_CELLS: tuple[tuple[str, str | None, str], ...] = (
    ("SessionStart", None, "context"),
    ("PreToolUse", "Bash", "deny"),
    ("PreToolUse", "Bash", "rewrite"),
    ("PreToolUse", "apply_patch", "deny"),
    ("PreToolUse", "apply_patch", "rewrite"),
    ("PermissionRequest", "Bash", "deny"),
    ("PermissionRequest", "Bash", "permit"),
    ("PostToolUse", "Bash", "block"),
    ("Stop", None, "block"),
    ("SubagentStop", None, "block"),
)
PROVISIONAL_PROFILE = "windows-desktop-26.1021.31345-0.164.0-alpha.11.1"
_LOW_RISK_ACTIONS = frozenset({"deny", "block", "context"})

CODEX_CELLS: Mapping[str, tuple[tuple[str, str | None, str], ...]] = {
    "windows-cli-0.160.0": (
        ("SessionStart", None, "context"),
        ("UserPromptSubmit", None, "context"),
        ("PostToolUse", "Bash", "context"),
        ("PreToolUse", "Bash", "deny"),
        ("PreToolUse", "Bash", "rewrite"),
        ("SubagentStop", None, "block"),
    ),
    "windows-cli-exec-0.162.0-alpha.17.2": (
        ("PreToolUse", "Bash", "context"),
        ("PreToolUse", "apply_patch", "context"),
        ("PreToolUse", "apply_patch", "deny"),
        ("SubagentStart", None, "context"),
        ("Stop", None, "block"),
    ),
    "windows-cli-interactive-0.162.0-alpha.17.2": (("PreToolUse", "apply_patch", "rewrite"),),
    # Codex desktop is a separate surface; only the cells WD-153 confirmed on this exact
    # app/backend pair.
    "windows-desktop-26.1016.62342-0.163.0-alpha.11.2": _DESKTOP_CELLS,
    # A newer app/backend pair that no WD-153 probe has covered. Only the actions whose
    # failure is silence (they cannot change a call or approve one) are carried over, and
    # the profile is demoted to log-only if a denied call is seen to finish (`demotion`).
    PROVISIONAL_PROFILE: tuple(cell for cell in _DESKTOP_CELLS if cell[2] in _LOW_RISK_ACTIONS),
}

PROVISIONAL_PROFILES: frozenset[str] = frozenset({PROVISIONAL_PROFILE})


def subscription_tools(
    provider: str, event: str, action: str, tool: str | None
) -> tuple[str | None, ...]:
    """Possible scoped tools for rule validation and discovery, before a request.

    A wildcard rule is narrowed to the union of probed tools. Runtime delivery
    still requires the exact deployment profile; discovery grants no permission.
    """
    if action == "log":
        return (tool,)
    if provider != "codex":
        return (tool,) if renderable(provider, event, action) else ()
    found = {
        scoped_tool
        for cells in CODEX_CELLS.values()
        for scoped_event, scoped_tool, scoped_action in cells
        if (event, action) == (scoped_event, scoped_action)
        and (tool is None or tool == scoped_tool)
    }
    return tuple(sorted(found, key=lambda value: value or ""))


@dataclass(frozen=True, slots=True)
class Context:
    """What a rule sees for one hook call; read-only.

    ``session`` is the daemon's in-memory state of the calling agent, or None when
    nothing was observed for it yet (unknown is not zero).
    """

    paths: UserPaths
    config: Config
    provider: str
    hook_input: Event
    session: SessionState | None = None

    def readonly(self) -> Context:
        """Detach mutable collections and expose a recursively read-only snapshot."""
        return Context(
            self.paths,
            _readonly(self.config),
            self.provider,
            _readonly(self.hook_input),
            self.session,
        )


@dataclass(frozen=True, slots=True)
class Decision:
    """One rule's answer for one hook call. ``allow`` means "no opinion"."""

    action: Action = "allow"
    rule: str | None = None
    rule_version: str | None = None
    reason: str | None = None
    updated_input: Mapping[str, object] | None = None
    context: str | None = None
    project_id: UUID | None = None
    capability_gap: str | None = None
    requested_action: str | None = None
    codex_control_profile: str | None = None


ALLOW = Decision()


class Rule(Protocol):
    """The sole Python execution interface; modules implement this function."""

    def evaluate(self, ctx: Context) -> Decision: ...


def renderable(
    provider: str,
    event: str,
    action: str,
    *,
    tool: object = None,
    codex_control_profile: object = None,
) -> bool:
    """Whether the adapter can render ``action`` for ``event`` on ``provider``."""
    if provider != "codex":
        return action in _RENDERABLE.get(provider, {}).get(event, frozenset())
    if not _WINDOWS or not isinstance(codex_control_profile, str):
        return False
    return any(
        (event, action) == (scoped_event, scoped_action)
        and (scoped_tool is None or tool == scoped_tool)
        for scoped_event, scoped_tool, scoped_action in CODEX_CELLS.get(codex_control_profile, ())
    )
