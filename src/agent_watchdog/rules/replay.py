"""Offline replay of one approved rule against a stored session (WD-143/144)."""

import hashlib
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from agent_watchdog import inspection
from agent_watchdog.config import Config, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.rules.api import Context, Decision
from agent_watchdog.rules.declarative import DeclarativeRule, evaluate
from agent_watchdog.rules.manage import RulesError, find
from agent_watchdog.rules.python import PythonHost, PythonRule
from agent_watchdog.state import SessionState, apply, initial, key_of


def _stored_session(
    paths: UserPaths, config: Config, session: str
) -> tuple[str, str, list[Envelope]]:
    matches: list[tuple[str, str, list[Envelope]]] = []
    for project in config.projects:
        try:
            with inspection.database(paths, project) as db:
                rows = db.execute(
                    "SELECT envelope FROM events WHERE session_id=? ORDER BY rowid", (session,)
                ).fetchall()
        except Exception as error:
            # Projects without a collected database are ordinary. Other failures
            # should remain visible instead of silently hiding a candidate scope.
            from agent_watchdog.storage import StorageError

            if isinstance(error, StorageError) and "no collected database" in str(error):
                continue
            raise
        events = [inspection.persisted_envelope(row[0]) for row in rows]
        providers = {event.provider for event in events}
        if events:
            matches.append(
                (str(project.id), next(iter(providers)) if len(providers) == 1 else "", events)
            )
    if not matches:
        raise RulesError("Unknown session")
    scopes = {(project_id, provider) for project_id, provider, _ in matches}
    if len(scopes) != 1 or not next(iter(scopes))[1]:
        raise RulesError("Session ID is ambiguous across project/provider scopes")
    project_id, provider, events = matches[0]
    if any(str(event.project_id) != project_id or event.session_id != session for event in events):
        raise RulesError("Stored session identity is inconsistent")
    return project_id, provider, events


def _hook_input(event: Envelope) -> dict[str, object]:
    payload = event.payload.get(event.provider)
    if not isinstance(payload, Mapping):
        return {}
    metadata = payload.get("metadata")
    content = payload.get("content")
    result: dict[str, object] = dict(metadata) if isinstance(metadata, Mapping) else {}
    if isinstance(content, Mapping):
        result.update(content)
    result["hook_event_name"] = result.get("hook_event_name") or payload.get("hook_event_name")
    result["tool_name"] = result.get("tool_name") or payload.get("tool_name")
    return result


def _decision_json(decision: Decision) -> dict[str, object]:
    return {
        "action": decision.action,
        "rule": decision.rule,
        "rule_version": decision.rule_version,
        "reason": decision.reason,
        "context": decision.context,
        "updated_input": decision.updated_input,
    }


def _unexpired(rule: DeclarativeRule, event: Envelope) -> bool:
    expires_at = rule.action.expires_at
    return expires_at is None or event.received_at < datetime.fromisoformat(expires_at)


def replay(paths: UserPaths, config: Config, name: str, session: str) -> dict[str, Any]:
    """Evaluate one rule over stored hook observations without changing live state."""
    entry = find(paths, config, name)
    rule = entry.rule
    if rule is None:
        raise RulesError(entry.error or "The rule is invalid")
    python_rule = isinstance(rule, PythonRule)
    if entry.status == "invalid":
        raise RulesError(entry.error or "The rule is invalid")
    if entry.source != "builtin" and (
        entry.digest is None or config.rules.approved.get(name) != entry.digest
    ):
        raise RulesError("Rule must be approved for its current bytes before replay")
    if python_rule:
        snapshot = entry.snapshot
        approved_digest = config.rules.approved.get(name)
        if snapshot is None or entry.digest is None:
            raise RulesError("Python rule has no valid byte snapshot")
        if (
            approved_digest != entry.digest
            or hashlib.sha256(snapshot).hexdigest() != approved_digest
        ):
            raise RulesError("Python rule must be approved for its current bytes before replay")
        assert isinstance(rule, PythonRule)
    else:
        snapshot = None
        approved_digest = None

    project_id, provider, events = _stored_session(paths, config, session)
    states: dict[tuple[str, str, str], SessionState] = {}
    outcomes: list[dict[str, object]] = []
    diagnostics: list[dict[str, str]] = []
    host = PythonHost() if python_rule else None

    for event in events:
        document = event.model_dump(mode="json")
        key = key_of(document)
        state = states.get(key) if key else None
        hook_input = _hook_input(event)
        is_hook = event.source == "hook"
        matches = (
            is_hook
            and event.kind
            in (
                "tool.start",
                "tool.finish",
                "turn.start",
                "turn.end",
                "session.start",
                "session.end",
                "waiting",
                "interrupt",
                "compaction.start",
                "compaction.end",
                "agent.start",
                "agent.end",
            )
            and rule.event == hook_input.get("hook_event_name")
            and provider in rule.provider
            and rule.match.matches_tool(hook_input.get("tool_name"))
            and _unexpired(rule, event)
        )
        answer = Decision()
        if matches:
            ctx = Context(paths, config, provider, hook_input, state)
            if python_rule:
                assert host is not None and snapshot is not None and isinstance(rule, PythonRule)

                event_id = str(event.event_id)

                def record_error(
                    rule_name: str, error: Exception, *, event_id: str = event_id
                ) -> None:
                    diagnostics.append(
                        {
                            "event_id": event_id,
                            "rule": rule_name,
                            "error": type(error).__name__,
                        }
                    )

                answer = host.evaluate(rule, snapshot, approved_digest, ctx, on_error=record_error)
            else:
                answer = evaluate(rule, ctx, now=event.received_at)
        if is_hook:
            outcomes.append(
                {
                    "event_id": str(event.event_id),
                    "provider": event.provider,
                    "event": hook_input.get("hook_event_name"),
                    "received_at": event.received_at.isoformat(),
                    "matched": bool(matches),
                    "decision": _decision_json(answer),
                }
            )
        if key is not None:
            states[key] = apply(state or initial(*key), document)

    return {
        "name": name,
        "session": session,
        "project_id": project_id,
        "provider": provider,
        "replay": True,
        "scope": (
            "rule evaluation only; cooldowns, per-turn caps, daemon gates, "
            "and delivery are not simulated"
        ),
        "outcomes": outcomes,
        "diagnostics": diagnostics,
    }
