"""Rule host for the decision channel: the first delivered action wins.

The engine owns what spans calls (cooldowns, per-turn caps) and what needs the
configured project (kill switches). Rules themselves are data (see
``declarative``). The expensive step, resolving the project from ``cwd``, runs
only after a rule has fired, so a call no rule matches costs a state read and
a few comparisons.
"""

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

from agent_watchdog.config import Config, Project, UserPaths
from agent_watchdog.demotion import is_demoted
from agent_watchdog.registry import Registry
from agent_watchdog.rules.api import (
    ALLOW,
    CODEX_CELLS,
    PROVISIONAL_PROFILES,
    Context,
    Decision,
    renderable,
)
from agent_watchdog.rules.declarative import DeclarativeRule, evaluate
from agent_watchdog.rules.python import PythonHost, PythonRule
from agent_watchdog.rules.registry import RuleRegistry
from agent_watchdog.state import SessionState

SAME_MODEL_RULE = "subagent_same_model"
_THROTTLE_LIMIT = 4096
_THROTTLE_RETENTION_SECONDS = 24 * 3600

ErrorHandler = Callable[[str, Exception], None]


@dataclass(slots=True)
class _Fired:
    at: float
    turn: str | None
    in_turn: int


class _Throttle:
    """Cooldown and per-turn caps, per rule and agent, in memory only.

    A restart forgets them, which can only repeat a message, never lose a
    decision. An unknown turn is one bucket that never resets, so a cap errs
    towards firing less.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fired: dict[tuple[str, str, str, str], _Fired] = {}

    @staticmethod
    def _key(rule: DeclarativeRule, ctx: Context) -> tuple[str, str, str, str]:
        session, agent = ctx.hook_input.get("session_id"), ctx.hook_input.get("agent_id")
        return (
            rule.name,
            ctx.provider,
            session if isinstance(session, str) else "",
            agent if isinstance(agent, str) else "",
        )

    @staticmethod
    def _turn(ctx: Context) -> str | None:
        return ctx.session.turn_id if ctx.session is not None else None

    def permits(self, rule: DeclarativeRule, ctx: Context, now: float) -> bool:
        action = rule.action
        if not action.cooldown_seconds and action.max_per_turn is None:
            return True
        with self._lock:
            fired = self._fired.get(self._key(rule, ctx))
        if fired is None:
            return True
        if action.cooldown_seconds and now - fired.at < action.cooldown_seconds:
            return False
        capped = action.max_per_turn is not None and fired.in_turn >= action.max_per_turn
        return not (capped and fired.turn == self._turn(ctx))

    def record(self, rule: DeclarativeRule, ctx: Context, now: float) -> None:
        action = rule.action
        if not action.cooldown_seconds and action.max_per_turn is None:
            return
        key, turn = self._key(rule, ctx), self._turn(ctx)
        with self._lock:
            fired = self._fired.get(key)
            count = fired.in_turn + 1 if fired is not None and fired.turn == turn else 1
            self._fired[key] = _Fired(now, turn, count)
            if len(self._fired) > _THROTTLE_LIMIT:
                cutoff = now - _THROTTLE_RETENTION_SECONDS
                self._fired = {k: v for k, v in self._fired.items() if v.at >= cutoff}


@dataclass(slots=True)
class Engine:
    registry: RuleRegistry
    throttle: _Throttle
    gaps: dict[tuple[str, str, str, str, str], float] = field(default_factory=dict)
    gap_lock: threading.Lock = field(default_factory=threading.Lock)
    python: PythonHost = field(default_factory=PythonHost)

    def record_gap(
        self, rule: DeclarativeRule, profile: object, project: Project, provider: str
    ) -> bool:
        """At most one gap per rule/version/profile/project per ten minutes.

        Unrecognized attribution shares one bucket; payload data cannot grow the
        keys. Gap reporting never consumes the actual control action's throttle.
        """
        known = profile if isinstance(profile, str) and profile in CODEX_CELLS else "unknown"
        key = (rule.name, rule.version, known, str(project.id), provider)
        now = time.monotonic()
        with self.gap_lock:
            previous = self.gaps.get(key)
            if previous is not None and now - previous < 600:
                return False
            if len(self.gaps) >= _THROTTLE_LIMIT:
                self.gaps.pop(next(iter(self.gaps)))
            self.gaps[key] = now
        return True


_engines: dict[Path, Engine] = {}
_engines_lock = threading.Lock()


def engine_for(paths: UserPaths) -> Engine:
    """The rule registry and throttle of one configuration location."""
    with _engines_lock:
        found = _engines.get(paths.config)
        if found is None:
            found = _engines[paths.config] = Engine(RuleRegistry(paths), _Throttle())
        return found


def subscriptions(paths: UserPaths, config: Config) -> list[dict[str, str | None]]:
    """What the adapter should ask the daemon about, for the rules that may run now.

    Nothing while the global kill switch is off, so adapters stop paying a round
    trip for answers that could only be ``allow``.
    """
    if not config.defaults.policy_intervene:
        return []
    engine = engine_for(paths)
    excluded = frozenset(
        entry.name
        for entry in engine.registry.active(config)
        if engine.python.status(entry.name) == "disabled"
    )
    return engine.registry.subscriptions(config, excluded=excluded)


def _project_of(config: Config, hook_input: Mapping[str, object]) -> Project | None:
    cwd = hook_input.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        resolution = Registry(config).resolve(Path(cwd), timeout=0.5)
    except OSError, ValueError:
        return None
    if resolution is None:
        return None
    return next((item for item in config.projects if item.id == resolution.project_id), None)


def _applies(rule: DeclarativeRule, provider: str, event: str, tool: object) -> bool:
    return rule.event == event and provider in rule.provider and rule.match.matches_tool(tool)


def decide(
    paths: UserPaths,
    config: Config,
    provider: str,
    hook_input: Mapping[str, object],
    *,
    session: SessionState | None = None,
    on_error: ErrorHandler | None = None,
    codex_control_profile: object = None,
) -> Decision:
    """Return the first decision the adapter can actually deliver, else ``allow``.

    Only actions the adapter renders for this provider and event are returned,
    with Codex limited to explicitly selected installation profiles.
    ``Stop`` and ``SubagentStop`` never see a block
    while ``stop_hook_active`` is set, so a block cannot loop the harness. A
    ``log`` decision is returned only when no earlier rule delivered an action.
    An unresolved project or a switched-off project answers ``allow``: a false
    action is the one failure that would actually stop the harness.
    """
    event = hook_input.get("hook_event_name")
    if not isinstance(event, str):
        return ALLOW
    if not config.defaults.policy_intervene:
        return ALLOW
    if (
        event in {"Stop", "SubagentStop"}
        and "stop_hook_active" in hook_input
        and hook_input["stop_hook_active"] is not False
    ):
        return ALLOW
    engine = engine_for(paths)
    context = Context(paths, config, provider, hook_input, session)
    now, mono = datetime.now(UTC), time.monotonic()
    project: Project | None = None
    looked_up = False
    logged: Decision | None = None
    for entry in engine.registry.active(config):
        rule = entry.rule
        if rule is None or not _applies(rule, provider, event, hook_input.get("tool_name")):
            continue
        if rule.action.expires_at is not None and now >= datetime.fromisoformat(
            rule.action.expires_at
        ):
            continue
        try:
            if isinstance(rule, PythonRule):
                # Python initialization itself can have effects: resolve the kill
                # switch before any approved code is imported or evaluated.
                if not looked_up:
                    project, looked_up = _project_of(config, hook_input), True
                if project is None or not project.overrides.apply(config.defaults).policy_intervene:
                    return ALLOW
                decision = engine.python.evaluate(
                    rule,
                    entry.snapshot or b"",
                    config.rules.approved.get(rule.name),
                    context,
                    on_error=on_error,
                )
            else:
                decision = evaluate(rule, context, now=now)
        except Exception as error:  # noqa: BLE001 - one broken rule must not silence the others
            if on_error is not None:
                on_error(rule.name, error)
            continue
        if decision.action == "allow":
            continue
        gap = decision.action != "log" and not renderable(
            provider,
            event,
            decision.action,
            tool=hook_input.get("tool_name"),
            codex_control_profile=codex_control_profile,
        )
        # A provisional profile that was seen to fail is log-only until re-probed.
        demoted = (
            decision.action != "log"
            and not gap
            and provider == "codex"
            and isinstance(codex_control_profile, str)
            and codex_control_profile in PROVISIONAL_PROFILES
            and is_demoted(paths, codex_control_profile)
        )
        gap = gap or demoted
        # Bash and patch rewrites have a string command contract. An arbitrary
        # replacement object is not evidence that the provider can apply it.
        original_input = hook_input.get("tool_input")
        if (
            provider == "codex"
            and decision.action == "rewrite"
            and (
                not isinstance(original_input, Mapping)
                or not isinstance(original_input.get("command"), str)
                or decision.updated_input is None
                or not isinstance(decision.updated_input.get("command"), str)
            )
        ):
            gap = True
        if not engine.throttle.permits(rule, context, mono):
            continue
        if not looked_up:
            project, looked_up = _project_of(config, hook_input), True
        if project is None:
            return ALLOW
        limits = project.overrides.apply(config.defaults)
        # Two independent switches, each of which stops every action: a project
        # cannot switch the channel back on once the global default is off.
        if not (config.defaults.policy_intervene and limits.policy_intervene):
            return ALLOW
        if gap:
            if engine.record_gap(rule, codex_control_profile, project, provider):
                logged = logged or replace(
                    decision,
                    action="log",
                    project_id=project.id,
                    requested_action=decision.action,
                    capability_gap="profile_demoted"
                    if demoted
                    else "unsupported_codex_cell"
                    if provider == "codex"
                    else "unsupported_cell",
                    codex_control_profile=codex_control_profile
                    if isinstance(codex_control_profile, str)
                    and codex_control_profile in CODEX_CELLS
                    else None,
                    reason=(
                        f"Watchdog: requested control is unavailable for this {provider} "
                        "deployment or cell."
                    ),
                    context=None,
                    updated_input=None,
                )
            continue
        engine.throttle.record(rule, context, mono)
        decision = replace(
            decision,
            project_id=project.id,
            codex_control_profile=codex_control_profile
            if provider == "codex"
            and isinstance(codex_control_profile, str)
            and codex_control_profile in CODEX_CELLS
            else None,
        )
        if decision.action == "log":
            logged = logged or decision
            continue
        return decision
    return logged or ALLOW
