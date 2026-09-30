"""LLM-assisted insights over collected observations (WD-123).

Each mode builds a deterministic, redacted evidence bundle from a read-only
snapshot, sends it to one isolated `claude -p` call only when the user runs the
command explicitly, validates the structured answer, and flags any claim that
cites evidence absent from the bundle. A model answer never changes stored
observations, findings, labels, or verdicts.
"""

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_watchdog.config import Project, UserPaths, load_config
from agent_watchdog.files import atomic_write
from agent_watchdog.insights import (
    budget,
    bundle,
    context,
    contract,
    errors,
    llm,
    permissions,
    render,
    scope,
    session,
    sessions,
    subagents,
    tokens,
    workflow,
)
from agent_watchdog.storage import StorageError

MODES = {
    module.MODE: module
    for module in (
        errors,
        context,
        tokens,
        workflow,
        subagents,
        permissions,
        session,
        sessions,
    )
}
DEFAULT_MODEL = "sonnet"
DEFAULT_WINDOW = timedelta(days=7)
DEFAULT_MAX_BUNDLE_TOKENS = budget.DEFAULT_MAX_BUNDLE_TOKENS
# Latency is dominated by the answer (a 13K-token answer took 148 s); a window-scaled
# bundle adds input time, so allow well beyond the measured call.
DEFAULT_TIMEOUT = 600.0
TASK = (
    "Analyze the Watchdog evidence bundle below and answer in the required structure.\n\n"
    "<bundle>\n{bundle}\n</bundle>\n"
)


def _window(
    *,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
    max_bundle_tokens: int | None,
    timeout: float,
    output: Path | None,
    now: datetime,
) -> tuple[dict[str, Any], datetime | None, Path | None]:
    if (max_bundle_tokens is not None and max_bundle_tokens < 1000) or timeout <= 0:
        raise StorageError("Require --max-bundle-tokens >= 1000 and a positive --timeout")
    if output is not None:
        output = output.resolve()
        if output.exists():
            raise StorageError("Insights report already exists")
    if since is None and session_id is None:
        since = now - DEFAULT_WINDOW
    window = {
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "provider": provider,
        "session_id": session_id,
    }
    return window, since, output


def run(
    paths: UserPaths,
    project: Project,
    *,
    alias: str,
    mode: str,
    provider: str | None,
    session_id: str | None,
    since: datetime | None,
    until: datetime | None,
    model: str,
    effort: str | None,
    timeout: float,
    max_bundle_tokens: int | None,
    language: str,
    dry_run: bool,
    output: Path | None,
    runner: llm.Runner,
) -> dict[str, Any]:
    module = MODES[mode]
    if session_id is not None and provider is None:
        raise StorageError("Select --provider with --session")
    if getattr(module, "PROJECT_WIDE", False) and session_id is not None:
        raise StorageError(f"The {mode} mode covers the whole project; drop --session")
    if getattr(module, "REQUIRES_SESSION", False) and session_id is None:
        raise StorageError(f"Select --provider and --session for the {mode} mode")
    now = datetime.now(UTC)
    window, since, output = _window(
        provider=provider,
        session_id=session_id,
        since=since,
        until=until,
        max_bundle_tokens=max_bundle_tokens,
        timeout=timeout,
        output=output,
        now=now,
    )
    draft = module.build(
        paths, project, provider=provider, session_id=session_id, since=since, until=until
    )

    def enabled() -> bool:
        limits = project.overrides.apply(load_config(paths.config).defaults)
        return limits.insights_llm_enabled

    return _respond(
        paths,
        module,
        draft,
        label=alias,
        cross=False,
        window=window,
        now=now,
        enabled=enabled,
        model=model,
        effort=effort,
        timeout=timeout,
        max_bundle_tokens=max_bundle_tokens,
        language=language,
        dry_run=dry_run,
        output=output,
        runner=runner,
    )


def run_all(
    paths: UserPaths,
    *,
    mode: str,
    provider: str | None,
    since: datetime | None,
    until: datetime | None,
    model: str,
    effort: str | None,
    timeout: float,
    max_bundle_tokens: int | None,
    language: str,
    dry_run: bool,
    output: Path | None,
    runner: llm.Runner,
) -> dict[str, Any]:
    """Run one mode over every registered project that allows insights (WD-132)."""
    if mode not in scope.CROSS_MODES:
        raise StorageError(f"--all-projects supports only {', '.join(scope.CROSS_MODES)}")
    # Only the cross-project modes define `analyze`, so the union of mode modules is untyped here.
    module: Any = MODES[mode]
    now = datetime.now(UTC)
    window, since, output = _window(
        provider=provider,
        session_id=None,
        since=since,
        until=until,
        max_bundle_tokens=max_bundle_tokens,
        timeout=timeout,
        output=output,
        now=now,
    )
    sources, selection = scope.collect(
        module, paths, load_config(paths.config), provider=provider, since=since, until=until
    )
    draft = scope.annotate(module.analyze(sources), selection)
    result = _respond(
        paths,
        module,
        draft,
        label=f"{len(selection.included)} projects: {', '.join(selection.included)}",
        cross=True,
        window=window,
        now=now,
        enabled=lambda: bool(selection.included),
        model=model,
        effort=effort,
        timeout=timeout,
        max_bundle_tokens=max_bundle_tokens,
        language=language,
        dry_run=dry_run,
        output=output,
        runner=runner,
    )
    if result["reason"] == "disabled":
        result["reason"] = "no_eligible_projects"
    # Skipped names stay local: the bundle only counts them.
    return result | {
        "projects": {
            "included": selection.included,
            "disabled": selection.disabled,
            "unreadable": selection.unreadable,
        }
    }


def _respond(
    paths: UserPaths,
    module: Any,
    draft: bundle.Draft,
    *,
    label: str,
    cross: bool,
    window: dict[str, Any],
    now: datetime,
    enabled: Callable[[], bool],
    model: str,
    effort: str | None,
    timeout: float,
    max_bundle_tokens: int | None,
    language: str,
    dry_run: bool,
    output: Path | None,
    runner: llm.Runner,
) -> dict[str, Any]:
    mode = module.MODE
    budget_plan = budget.plan(paths, model, max_bundle_tokens)
    fitted = bundle.fit(
        draft, mode=mode, window=window, max_tokens=budget_plan["max_bundle_tokens"]
    )
    text = bundle.dumps(fitted)
    result: dict[str, Any] = {
        "project": label,
        "mode": mode,
        "status": "dry_run",
        "reason": None,
        "generated_at": now.isoformat(),
        "window": window,
        "facts": fitted["facts"],
        "coverage": fitted["coverage"],
        "estimated_bundle_tokens": bundle.estimate_tokens(text),
        "budget": budget_plan,
        "summary": None,
        "recommendations": [],
        "rule_candidates": [],
        "provenance": {},
        "output": None,
    }
    if dry_run:
        return result | {"bundle": fitted}

    if not enabled():
        return result | {"status": "unavailable", "reason": "disabled"}
    output_model = module.CrossOutput if cross else module.Output
    answer = runner(
        llm.Request(
            system_prompt=contract.system_prompt(
                module.PROMPT + (scope.ADDENDUM if cross else ""), language
            ),
            prompt=TASK.format(bundle=text),
            schema=contract.json_schema(output_model),
            model=model,
            effort=effort,
            timeout=timeout,
        )
    )
    budget.remember(paths, model, answer.provenance, now)
    result["provenance"] = answer.provenance | {
        "bundle_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "bundle_bytes": len(text.encode("utf-8")),
    }
    if answer.reason is not None:
        return result | {"status": "unavailable", "reason": answer.reason}
    try:
        parsed = output_model.model_validate(answer.output)
    except ValidationError:
        return result | {"status": "unavailable", "reason": "malformed_output"}
    known = bundle.evidence_ids(fitted)
    content = parsed.model_dump(mode="json")
    recommendations = contract.ground(content["recommendations"], known)
    result |= {
        "status": "ok",
        "summary": content["summary"],
        "recommendations": scope.enforce(recommendations, fitted) if cross else recommendations,
        "rule_candidates": contract.ground(content["rule_candidates"], known),
    }
    # Mode-specific answers, such as the session judgement or the sessions triage lists,
    # are grounded the same way; a mode may then resolve the ids they cite.
    resolve = getattr(module, "resolve", None)
    for key, value in content.items():
        if key in result:
            continue
        if isinstance(value, dict):
            result[key] = contract.ground([value], known)[0]
        elif isinstance(value, list) and all(isinstance(entry, dict) for entry in value):
            grounded = contract.ground(value, known)
            result[key] = resolve(grounded, fitted, label) if resolve else grounded
    if output is not None:
        result["output"] = str(output)
        atomic_write(output, render.markdown(result).encode("utf-8"))
    return result
