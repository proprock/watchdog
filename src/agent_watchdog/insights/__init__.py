"""LLM-assisted insights over collected observations (WD-123).

Each mode builds a deterministic, redacted evidence bundle from a read-only
snapshot, sends it to one isolated `claude -p` call only when the user runs the
command explicitly, validates the structured answer, and flags any claim that
cites evidence absent from the bundle. A model answer never changes stored
observations, findings, labels, or verdicts.
"""

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from agent_watchdog.config import Project, UserPaths, load_config
from agent_watchdog.files import atomic_write
from agent_watchdog.insights import budget, bundle, contract, errors, llm, render
from agent_watchdog.storage import StorageError

MODES = {errors.MODE: errors}
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
    if (max_bundle_tokens is not None and max_bundle_tokens < 1000) or timeout <= 0:
        raise StorageError("Require --max-bundle-tokens >= 1000 and a positive --timeout")
    if output is not None:
        output = output.resolve()
        if output.exists():
            raise StorageError("Insights report already exists")
    now = datetime.now(UTC)
    if since is None and session_id is None:
        since = now - DEFAULT_WINDOW
    window = {
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "provider": provider,
        "session_id": session_id,
    }
    draft = module.build(
        paths, project, provider=provider, session_id=session_id, since=since, until=until
    )
    budget_plan = budget.plan(paths, model, max_bundle_tokens)
    fitted = bundle.fit(
        draft, mode=mode, window=window, max_tokens=budget_plan["max_bundle_tokens"]
    )
    text = bundle.dumps(fitted)
    result: dict[str, Any] = {
        "project": alias,
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

    limits = project.overrides.apply(load_config(paths.config).defaults)
    if not limits.insights_llm_enabled:
        return result | {"status": "unavailable", "reason": "disabled"}
    answer = runner(
        llm.Request(
            system_prompt=contract.system_prompt(module.PROMPT, language),
            prompt=TASK.format(bundle=text),
            schema=contract.json_schema(module.Output),
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
        parsed = module.Output.model_validate(answer.output)
    except ValidationError:
        return result | {"status": "unavailable", "reason": "malformed_output"}
    known = bundle.evidence_ids(fitted)
    content = parsed.model_dump(mode="json")
    result |= {
        "status": "ok",
        "summary": content["summary"],
        "recommendations": contract.ground(content["recommendations"], known),
        "rule_candidates": contract.ground(content["rule_candidates"], known),
    }
    if output is not None:
        result["output"] = str(output)
        atomic_write(output, render.markdown(result).encode("utf-8"))
    return result
