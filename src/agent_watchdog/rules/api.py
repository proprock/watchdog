"""Decision vocabulary shared by the daemon's decision channel and its rules."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from agent_watchdog.config import Config, UserPaths
from agent_watchdog.state import SessionState

# ``log`` records a finding and sends nothing: the adapter is told ``allow``.
Action = Literal["allow", "log", "deny", "ask", "rewrite", "context", "block"]

ACTIONS: frozenset[str] = frozenset({"allow", "log", "deny", "ask", "rewrite", "context", "block"})

# The provider/event/action cells the native adapter can render, limited to the
# cells WD-139 confirmed live on Claude Code (docs/provider-compatibility.md,
# "Control capability"). Anything else is silence there, so the daemon never
# reports an action as delivered that the adapter would drop. Codex is
# intentionally absent until WD-151 confirms which cells it honours; nothing is
# returned to Codex without live evidence. Keep in step with `render` in
# native/src/main.rs.
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


@dataclass(frozen=True, slots=True)
class Context:
    """What a rule sees for one hook call; read-only.

    ``session`` is the daemon's in-memory state of the calling agent, or None when
    nothing was observed for it yet (unknown is not zero).
    """

    paths: UserPaths
    config: Config
    provider: str
    hook_input: Mapping[str, object]
    session: SessionState | None = None


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


ALLOW = Decision()


def renderable(provider: str, event: str, action: str) -> bool:
    """Whether the adapter can render ``action`` for ``event`` on ``provider``."""
    return action in _RENDERABLE.get(provider, {}).get(event, frozenset())
