"""Declarative rules: TOML data with a closed set of predicates and actions (WD-142).

A rule file never executes anything. It names a hook event, optionally narrows
it by tool name and by regular expressions over the tool input, optionally adds
predicates over the daemon's session state, and says which action to take. The
predicate list is closed: an unknown key is an error at load, never ignored.
"""

import copy
import json
import re
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import cache
from typing import Any, Literal, Self

from pydantic import ValidationError, field_validator, model_validator

from agent_watchdog.analysis import model_family_matches
from agent_watchdog.config import RuleName
from agent_watchdog.models import NonNegative, Positive, StrictModel, Versioned
from agent_watchdog.rules.api import ALLOW, Context, Decision, renderable
from agent_watchdog.state import SessionState, trailing_repeats

# The scan window for `input_regex`/`error_regex`, so a pathological pattern meets
# bounded input; together with the length and nesting limits below it replaces a
# regex timeout, which `re` does not offer.
INPUT_LIMIT = 64 * 1024
REGEX_LIMIT = 512
# A quantified group that itself contains a quantifier: `(a+)+`, `(.*)*`.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[*+][^()]*\)[*+{]")
PLACEHOLDERS = frozenset(
    {"repeats", "failures", "coordinator_model", "candidate_model", "lower_tier"}
)
LOWER_TIER = "$lower_tier"
_REWRITE_KEY = re.compile(r"^tool_input\.[A-Za-z_][A-Za-z0-9_]*$")

HookEvent = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "SubagentStop",
    "SessionStart",
    "PreCompact",
]


class RuleError(ValueError):
    """A rule body is invalid; the message never repeats the body."""


@cache
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def _checked_regex(pattern: str | None) -> str | None:
    if pattern is None:
        return None
    if len(pattern) > REGEX_LIMIT:
        raise ValueError(f"Regular expression is longer than {REGEX_LIMIT} characters")
    if _NESTED_QUANTIFIER.search(pattern):
        raise ValueError("Regular expression nests a quantifier inside a quantified group")
    try:
        re.compile(pattern)
    except re.error as error:
        raise ValueError("Regular expression does not compile") from error
    return pattern


class Match(StrictModel):
    tool_name: str | None = None
    input_regex: str | None = None
    error_regex: str | None = None

    @field_validator("input_regex", "error_regex")
    @classmethod
    def safe_regex(cls, value: str | None) -> str | None:
        return _checked_regex(value)


class Bounds(StrictModel):
    min: NonNegative | None = None
    max: NonNegative | None = None

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("Give min, max, or both")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min is greater than max")
        return self

    def holds(self, value: float | None) -> bool:
        if value is None:
            return False
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)


class StatePredicates(StrictModel):
    """The closed predicate set over the calling agent's ``SessionState``."""

    repeats_same_input_output: Bounds | None = None
    edits_since_last_call: Bounds | None = None
    failures_in_turn: Bounds | None = None
    compactions_in_turn: Bounds | None = None
    turn_minutes: Bounds | None = None
    edited_without_verification: bool | None = None
    subagent_same_tier_as_coordinator: bool | None = None

    def used(self) -> bool:
        return any(value is not None for value in self.__dict__.values())


class RuleAction(StrictModel):
    kind: Literal["log", "context", "ask", "rewrite", "deny", "block"]
    message: str
    rewrite: dict[str, str] | None = None
    cooldown_seconds: NonNegative = 0
    max_per_turn: Positive | None = None
    expires_at: str | None = None

    @field_validator("message")
    @classmethod
    def known_placeholders(cls, value: str) -> str:
        if not value.strip() or len(value) > 1000:
            raise ValueError("A message of 1 to 1000 characters is required")
        try:
            fields = list(string.Formatter().parse(value))
        except ValueError as error:
            raise ValueError("Message has unbalanced braces") from error
        for _, name, spec, conversion in fields:
            if name is not None and (name not in PLACEHOLDERS or spec or conversion):
                raise ValueError("Message uses an unknown placeholder")
        return value

    @field_validator("expires_at")
    @classmethod
    def aware_instant(cls, value: str | None) -> str | None:
        if value is not None:
            _instant(value)
        return value

    @model_validator(mode="after")
    def rewrite_only_for_rewrite(self) -> Self:
        if (self.kind == "rewrite") != bool(self.rewrite):
            raise ValueError("rewrite is required for, and only for, the rewrite action")
        for key in self.rewrite or {}:
            if not _REWRITE_KEY.fullmatch(key):
                raise ValueError("rewrite keys must look like tool_input.<field>")
        return self


class DeclarativeRule(Versioned):
    name: RuleName
    version: str
    origin: str
    description: str = ""
    provider: list[Literal["claude", "codex"]]
    event: HookEvent
    priority: int = 100
    # Built-ins only: false ships the rule switched off until `rules enable`.
    default_enabled: bool = True
    match: Match = Match()
    state: StatePredicates = StatePredicates()
    action: RuleAction

    @field_validator("version", "origin")
    @classmethod
    def short_text(cls, value: str) -> str:
        if not value.strip() or len(value) > 200:
            raise ValueError("A value of 1 to 200 characters is required")
        return value

    @field_validator("provider")
    @classmethod
    def at_least_one_provider(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("Name at least one provider")
        return value

    @model_validator(mode="after")
    def deliverable(self) -> Self:
        # Recording an action as sent while the adapter drops it would be a false
        # audit trail, so a rule may only ask for what some listed provider renders.
        kind = self.action.kind
        if kind != "log" and not any(renderable(p, self.event, kind) for p in self.provider):
            raise ValueError(f"The {kind} action cannot be delivered on {self.event}")
        return self


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError("Unsupported TOML value")


def _summary(error: ValidationError) -> str:
    parts = [
        f"{'.'.join(str(item) for item in problem['loc']) or 'rule'}: {problem['msg']}"
        for problem in error.errors(include_input=False, include_url=False)[:5]
    ]
    return "; ".join(parts)[:400]


def parse_rule(text: str) -> DeclarativeRule:
    """Validate one rule body; any problem raises ``RuleError``."""
    try:
        data = tomllib.loads(text)
        return DeclarativeRule.model_validate_json(
            json.dumps(data, allow_nan=False, default=_json_default)
        )
    except ValidationError as error:
        raise RuleError(_summary(error)) from None
    except (tomllib.TOMLDecodeError, ValueError, TypeError, RecursionError):
        raise RuleError("The rule file is not valid TOML") from None


def _instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("expires_at must be an ISO 8601 timestamp") from None
    if parsed.tzinfo is None:
        raise ValueError("expires_at needs a UTC offset")
    return parsed.astimezone(UTC)


def _input_text(tool_input: object) -> str | None:
    if not isinstance(tool_input, Mapping):
        return None
    command = tool_input.get("command")
    text = (
        command
        if isinstance(command, str)
        else json.dumps(tool_input, separators=(",", ":"), default=str)
    )
    return text[:INPUT_LIMIT]


def _turn_minutes(state: SessionState, now: datetime) -> float | None:
    if state.turn_started_at is None:
        return None
    try:
        started = _instant(state.turn_started_at)
    except ValueError:
        return None
    return max(0.0, (now - started).total_seconds() / 60)


@dataclass(frozen=True, slots=True)
class _Facts:
    """What the state predicates and message placeholders read for one call."""

    repeats: int | None = None
    failures: int | None = None
    coordinator_model: str | None = None
    candidate_model: str | None = None
    lower_tier: str | None = None
    same_tier: bool | None = None


def _facts(ctx: Context, tool_input: object) -> _Facts:
    state = ctx.session
    candidate = None
    if isinstance(tool_input, Mapping) and isinstance(tool_input.get("model"), str):
        candidate = tool_input["model"].strip().lower() or None
    coordinator = state.coordinator_model if state is not None else None
    tiers = ctx.config.rules.tiers
    lower = same = None
    if candidate in tiers and coordinator:
        index = tiers.index(candidate)
        same = model_family_matches(candidate, coordinator) and index > 0
        lower = tiers[index - 1] if index > 0 else None
    return _Facts(
        repeats=(trailing_repeats(state) or None) if state is not None else None,
        failures=state.failures_in_turn if state is not None else None,
        coordinator_model=coordinator,
        candidate_model=candidate,
        lower_tier=lower,
        same_tier=same,
    )


def _state_holds(
    predicates: StatePredicates, state: SessionState, facts: _Facts, now: datetime
) -> bool:
    checks: list[tuple[Any, Any]] = [
        (predicates.repeats_same_input_output, facts.repeats),
        (predicates.edits_since_last_call, state.edits_since_last_call),
        (predicates.failures_in_turn, state.failures_in_turn),
        (predicates.compactions_in_turn, state.compactions_in_turn),
        (predicates.turn_minutes, _turn_minutes(state, now)),
    ]
    if not all(bounds.holds(value) for bounds, value in checks if bounds is not None):
        return False
    wanted = predicates.edited_without_verification
    if wanted is not None and state.edited_without_verification != wanted:
        return False
    wanted = predicates.subagent_same_tier_as_coordinator
    return wanted is None or (facts.same_tier is not None and facts.same_tier == wanted)


def _message(rule: DeclarativeRule, facts: _Facts) -> str:
    values = {
        name: "unknown" if getattr(facts, name) is None else getattr(facts, name)
        for name in PLACEHOLDERS
    }
    return rule.action.message.format_map(values)


def evaluate(rule: DeclarativeRule, ctx: Context, *, now: datetime) -> Decision:
    """The rule's decision for one hook call; ``allow`` when it does not apply.

    Unknown state never matches: a missing session, an unobserved model or an
    unparsable timestamp makes the predicate false rather than guessing.
    Cooldowns and per-turn caps are the engine's, since they span calls.
    """
    hook = ctx.hook_input
    if hook.get("hook_event_name") != rule.event or ctx.provider not in rule.provider:
        return ALLOW
    if rule.match.tool_name is not None and hook.get("tool_name") != rule.match.tool_name:
        return ALLOW
    if rule.action.expires_at is not None and now >= _instant(rule.action.expires_at):
        return ALLOW
    tool_input = hook.get("tool_input")
    if rule.match.input_regex is not None:
        text = _input_text(tool_input)
        if text is None or _compiled(rule.match.input_regex).search(text) is None:
            return ALLOW
    if rule.match.error_regex is not None:
        error = hook.get("error")
        if (
            not isinstance(error, str)
            or _compiled(rule.match.error_regex).search(error[:INPUT_LIMIT]) is None
        ):
            return ALLOW
    facts = _facts(ctx, tool_input)
    if rule.state.used() and (
        ctx.session is None or not _state_holds(rule.state, ctx.session, facts, now)
    ):
        return ALLOW
    updated = _rewritten(rule, tool_input, facts)
    if rule.action.kind == "rewrite" and updated is None:
        return ALLOW
    message = _message(rule, facts)
    return Decision(
        action=rule.action.kind,
        rule=rule.name,
        rule_version=rule.version,
        reason=None if rule.action.kind == "context" else message,
        context=message if rule.action.kind == "context" else None,
        updated_input=updated,
    )


def _rewritten(
    rule: DeclarativeRule, tool_input: object, facts: _Facts
) -> dict[str, object] | None:
    """The full replacement input: every field of the original, the named ones changed."""
    if not rule.action.rewrite or not isinstance(tool_input, Mapping):
        return None
    updated = copy.deepcopy(dict(tool_input))
    for key, value in rule.action.rewrite.items():
        resolved = facts.lower_tier if value == LOWER_TIER else value
        if resolved is None:
            return None
        updated[key.removeprefix("tool_input.")] = resolved
    return updated
