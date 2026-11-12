"""The operations behind `agent-watchdog rules`: list, show, approve, reject, enable, stats.

Approval is the one security-relevant step. It records the SHA-256 of the exact
bytes the user was shown, re-checks that digest under the configuration lock,
and refuses a rule that does not validate, so a file cannot change between
review and approval and an invalid rule is never approved.
"""

import sqlite3
from collections import Counter
from collections.abc import Callable
from datetime import datetime

from agent_watchdog import daemon
from agent_watchdog.analysis import control_findings
from agent_watchdog.config import Config, Rules, UserPaths
from agent_watchdog.events import Envelope
from agent_watchdog.registry import Registry
from agent_watchdog.rules.python import PythonRule
from agent_watchdog.rules.registry import RuleEntry, RuleRegistry
from agent_watchdog.storage import StorageError


class RulesError(ValueError):
    """A rules command cannot be carried out; the message is safe to show."""


def _entries(paths: UserPaths, config: Config) -> tuple[RuleEntry, ...]:
    # A fresh registry: a command is one short process, and approval must read the
    # files as they are now, not as a cache last saw them.
    return RuleRegistry(paths).entries(config)


def find(paths: UserPaths, config: Config, name: str) -> RuleEntry:
    entry = next((item for item in _entries(paths, config) if item.name == name), None)
    if entry is None:
        raise RulesError("Unknown rule")
    return entry


def _describe(entry: RuleEntry) -> dict[str, object]:
    rule = entry.rule
    return {
        "name": entry.name,
        "source": entry.source,
        "status": entry.status,
        "version": rule.version if rule else None,
        "origin": rule.origin if rule else None,
        "event": rule.event if rule else None,
        "tool_name": rule.match.tool_name if rule else None,
        "tool_names": rule.match.tool_names if rule else None,
        "action": rule.action.kind if rule else None,
        "description": rule.description if rule else None,
        "error": entry.error,
        "format": "python" if isinstance(rule, PythonRule) else "toml",
    }


def listing(paths: UserPaths, config: Config) -> list[dict[str, object]]:
    fired = {item["rule"]: item["fired"] for item in stats(paths, config, since=None)}
    return [
        _describe(entry) | {"fired": fired.get(entry.name, 0)} for entry in _entries(paths, config)
    ]


def show(paths: UserPaths, config: Config, name: str) -> dict[str, object]:
    entry = find(paths, config, name)
    return _describe(entry) | {"digest": entry.digest, "text": entry.text}


def check_approvable(entry: RuleEntry) -> None:
    if entry.source == "builtin":
        raise RulesError("Built-in rules ship with the package and need no approval")
    if entry.rule is None or entry.digest is None:
        raise RulesError(entry.error or "The rule file is invalid")


def _mutate(paths: UserPaths, update: Callable[[Config], Rules]) -> Config:
    """Apply ``update`` to the rules section under the configuration lock."""
    saved: list[Config] = []

    def mutation(registry: Registry) -> None:
        rules = update(registry.config)
        registry.config = registry.config.model_copy(update={"rules": rules})
        saved.append(registry.config)

    daemon.mutate_registry(paths, mutation)
    return saved[0]


def _with(rules: Rules, **changes: object) -> Rules:
    return Rules.model_validate({**rules.model_dump(), **changes})


def approve(paths: UserPaths, name: str, reviewed_digest: str) -> dict[str, object]:
    def update(config: Config) -> Rules:
        entry = find(paths, config, name)
        check_approvable(entry)
        if entry.digest != reviewed_digest:
            raise RulesError("The rule file changed after it was shown; review it again")
        return _with(config.rules, approved={**config.rules.approved, name: reviewed_digest})

    config = _mutate(paths, update)
    return _describe(find(paths, config, name)) | {"digest": reviewed_digest}


def reject(paths: UserPaths, name: str) -> dict[str, object]:
    """Revoke a rule's approval and keep it off until `enable`."""

    def update(config: Config) -> Rules:
        find(paths, config, name)
        approved = {key: value for key, value in config.rules.approved.items() if key != name}
        disabled = [*dict.fromkeys([*config.rules.disabled, name])]
        enabled = [item for item in config.rules.enabled if item != name]
        return _with(config.rules, approved=approved, disabled=disabled, enabled=enabled)

    return _describe(find(paths, _mutate(paths, update), name))


def disable(paths: UserPaths, name: str) -> dict[str, object]:
    def update(config: Config) -> Rules:
        find(paths, config, name)
        disabled = [*dict.fromkeys([*config.rules.disabled, name])]
        enabled = [item for item in config.rules.enabled if item != name]
        return _with(config.rules, disabled=disabled, enabled=enabled)

    return _describe(find(paths, _mutate(paths, update), name))


def enable(paths: UserPaths, name: str) -> dict[str, object]:
    """Turn a rule on; it never approves a user rule, which still needs `approve`."""

    def update(config: Config) -> Rules:
        entry = find(paths, config, name)
        disabled = [item for item in config.rules.disabled if item != name]
        opt_in = isinstance(entry.rule, PythonRule) or (
            entry.source == "builtin" and entry.rule is not None and not entry.rule.default_enabled
        )
        enabled = (
            [*dict.fromkeys([*config.rules.enabled, name])] if opt_in else config.rules.enabled
        )
        return _with(config.rules, disabled=disabled, enabled=enabled)

    return _describe(find(paths, _mutate(paths, update), name))


def stats(paths: UserPaths, config: Config, *, since: datetime | None) -> list[dict[str, object]]:
    """Firings, verdicts, precision and delivery-stage coverage per rule.

    Precision is true positives over reviewed true and false positives and stays
    ``None`` while nothing is reviewed: unknown is not zero. Delivery coverage
    counts observed stages independently; a verdict never proves provider effect.
    """
    from agent_watchdog import inspection

    fired: Counter[str] = Counter()
    actions: dict[str, Counter[str]] = {}
    verdicts: dict[str, Counter[str]] = {}
    delivery: dict[str, Counter[str]] = {}
    for project in config.projects:
        try:
            with inspection.database(paths, project) as db:
                rows = db.execute(
                    "SELECT ef.provider, ef.session_id, e.envelope FROM events e "
                    "JOIN event_facts ef ON ef.event_id = e.event_id "
                    "WHERE ef.kind = 'control' "
                    "ORDER BY e.received_at, e.event_id"
                ).fetchall()
                sessions: dict[tuple[str, str], list[Envelope]] = {}
                for provider, session_id, document in rows:
                    event = Envelope.model_validate_json(document)
                    if since is None or event.received_at >= since:
                        sessions.setdefault((provider, session_id or ""), []).append(event)
                # Fetch only referenced acknowledgements, including hooks received
                # before --since. Loading all hook traffic would scale with the
                # entire observation history rather than the control history.
                referenced: set[str] = set()
                for controls in sessions.values():
                    for control in controls:
                        payload = control.payload.get(control.provider)
                        ids = payload.get("evidence_ids") if isinstance(payload, dict) else None
                        if isinstance(ids, list):
                            referenced.update(item for item in ids if isinstance(item, str))
                identifiers = sorted(referenced)
                for offset in range(0, len(identifiers), 900):
                    batch = identifiers[offset : offset + 900]
                    placeholders = ",".join("?" for _ in batch)
                    hook_rows = db.execute(
                        f"SELECT envelope FROM events WHERE event_id IN ({placeholders})",
                        batch,
                    ).fetchall()
                    for (document,) in hook_rows:
                        hook = Envelope.model_validate_json(document)
                        key = (hook.provider, hook.session_id or "")
                        if hook.source == "hook" and key in sessions:
                            sessions[key].append(hook)
                for (provider, session_id), events in sessions.items():
                    recorded = {
                        (item["rule"], item["rule_version"], item["fingerprint"]): item["verdict"]
                        for item in inspection.session_verdicts(db, provider, session_id)
                    }
                    for finding in control_findings(events):
                        rule = str(finding["rule"])
                        fired[rule] += 1
                        actions.setdefault(rule, Counter())[str(finding["action"])] += 1
                        is_attempt = finding["selected_action"] in {
                            "deny",
                            "ask",
                            "rewrite",
                            "context",
                            "block",
                            "permit",
                        }
                        if is_attempt:
                            coverage = delivery.setdefault(rule, Counter())
                            coverage["attempted"] += 1
                            if finding["response_sent_at"]:
                                coverage["response_sent"] += 1
                            if finding["adapter_rendered"] is True:
                                coverage["adapter_rendered"] += 1
                            elif finding["adapter_rendered"] is False:
                                coverage["adapter_unrenderable"] += 1
                            elif finding["adapter_rendered"] is None:
                                coverage["adapter_rendered_unknown"] += 1
                            effect = finding["provider_effect"]
                            if effect == "blocked":
                                coverage["provider_effect"] += 1
                            elif effect == "contradicted":
                                coverage["provider_effect_contradicted"] += 1
                            else:
                                coverage["provider_effect_unknown"] += 1
                            if finding["capability_gap"]:
                                coverage["capability_gaps"] += 1
                        verdict = recorded.get(
                            (rule, finding["rule_version"], finding["fingerprint"])
                        )
                        if verdict is not None:
                            verdicts.setdefault(rule, Counter())[verdict] += 1
        except StorageError, OSError, ValueError, sqlite3.Error:
            continue  # a project with no or a damaged database must not hide the others
    result: list[dict[str, object]] = []
    for rule in sorted(fired):
        counts = verdicts.get(rule, Counter())
        positive, negative = counts["true_positive"], counts["false_positive"]
        coverage = delivery.get(rule, Counter())
        result.append(
            {
                "rule": rule,
                "fired": fired[rule],
                "actions": dict(actions[rule]),
                "reviewed": positive + negative + counts["uncertain"],
                "true_positive": positive,
                "false_positive": negative,
                "uncertain": counts["uncertain"],
                "precision": round(positive / (positive + negative), 4)
                if positive + negative
                else None,
                "attempted": coverage["attempted"],
                "response_sent": coverage["response_sent"],
                "adapter_rendered": coverage["adapter_rendered"],
                "adapter_rendered_unknown": coverage["adapter_rendered_unknown"],
                "adapter_unrenderable": coverage["adapter_unrenderable"],
                "provider_effect": coverage["provider_effect"],
                "provider_effect_contradicted": coverage["provider_effect_contradicted"],
                "provider_effect_unknown": coverage["provider_effect_unknown"],
                "capability_gaps": coverage["capability_gaps"],
            }
        )
    return result
