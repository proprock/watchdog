"""Rule host for the decision channel: the first non-``allow`` answer wins."""

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

from agent_watchdog.analysis import model_family_matches
from agent_watchdog.config import Config, UserPaths
from agent_watchdog.registry import Registry
from agent_watchdog.rules.api import ALLOW, Decision, renderable
from agent_watchdog.storage import StorageError

SAME_MODEL_RULE = "subagent_same_model"

# The rule's deny reason tells the caller to downgrade the subagent's model;
# denying a spawn that is already at the lowest tier would ask for a
# downgrade with nowhere to go, contradicting the rule's own advice. Anthropic
# ranks opus > sonnet > haiku; the Agent tool's fourth alias, "fable", has no
# established public rank relative to the other three, so it is intentionally
# left out of this floor rather than guessed.
_LOWEST_TIER_ALIASES = frozenset({"haiku"})


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _same_model_subagent_spawn(
    paths: UserPaths, config: Config, provider: str, hook_input: Mapping[str, object]
) -> Decision:
    """Decide the WD-014 same-model-subagent-spawn rule for one PreToolUse call.

    Every failure path -- unresolved project, no database, no observed
    coordinator model -- returns ``allow``. A false deny is the one Watchdog
    failure that would actually stop the harness, so this never fails closed.
    A candidate already at the lowest known tier is never denied either, for
    the same reason: there is nothing lower to downgrade to (see
    ``_LOWEST_TIER_ALIASES``). The resolved project's
    ``policy_intervene_same_model_subagent_spawn`` (default ``True``,
    per-project overridable) is this rule's own kill switch -- set false to
    disable only this rule's `intervene` without pausing the daemon or
    affecting any other rule or the rule's own `log` half.
    """
    if (
        provider != "claude"
        or hook_input.get("hook_event_name") != "PreToolUse"
        or hook_input.get("tool_name") != "Agent"
    ):
        return ALLOW
    tool_input = hook_input.get("tool_input")
    candidate_model = _text(tool_input.get("model")) if isinstance(tool_input, dict) else None
    cwd, session_id = _text(hook_input.get("cwd")), _text(hook_input.get("session_id"))
    if candidate_model is None or cwd is None or session_id is None:
        return ALLOW
    if candidate_model.strip().lower() in _LOWEST_TIER_ALIASES:
        return ALLOW
    from agent_watchdog.inspection import database

    try:
        resolution = Registry(config).resolve(Path(cwd), timeout=0.5)
    except (OSError, ValueError):
        return ALLOW
    if resolution is None:
        return ALLOW
    project = next((item for item in config.projects if item.id == resolution.project_id), None)
    if project is None:
        return ALLOW
    limits = project.overrides.apply(config.defaults)
    if not limits.policy_intervene_same_model_subagent_spawn:
        return ALLOW
    try:
        with database(paths, project) as db:
            row = db.execute(
                "SELECT model FROM event_facts WHERE provider='claude' AND kind='usage' "
                "AND session_id=? AND agent_id IS NULL AND model IS NOT NULL "
                "ORDER BY COALESCE(occurred_at_us, received_at_us) DESC, event_id DESC LIMIT 1",
                (session_id,),
            ).fetchone()
    except (StorageError, sqlite3.Error, OSError):
        return ALLOW
    if row is None or not isinstance(row[0], str):
        return ALLOW
    if not model_family_matches(candidate_model, row[0]):
        return ALLOW
    return Decision(
        action="deny",
        rule=SAME_MODEL_RULE,
        reason=(
            f"Watchdog: this subagent would run on the same model tier ({candidate_model}) "
            "as its coordinating conversation. Downgrade the subagent's model."
        ),
        project_id=project.id,
    )


_RULES: tuple[Callable[[UserPaths, Config, str, Mapping[str, object]], Decision], ...] = (
    _same_model_subagent_spawn,
)


def decide(
    paths: UserPaths, config: Config, provider: str, hook_input: Mapping[str, object]
) -> Decision:
    """Return the first non-``allow`` decision the adapter can actually render.

    Only actions the adapter renders for this provider and event are returned,
    so a delivered action is never silently dropped on the other side; Codex
    gets ``allow`` until WD-151. ``Stop`` and ``SubagentStop`` never see a block
    while ``stop_hook_active`` is set, so a block cannot loop the harness.
    """
    event = hook_input.get("hook_event_name")
    if not isinstance(event, str):
        return ALLOW
    if event in {"Stop", "SubagentStop"} and hook_input.get("stop_hook_active") is True:
        return ALLOW
    for rule in _RULES:
        decision = rule(paths, config, provider, hook_input)
        if decision.action != "allow" and renderable(provider, event, decision.action):
            return decision
    return ALLOW
