"""Analyzer definitions: prompt building, versioning, and response parsing."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from typing import Any

from wdi.llm import context


def _template(name: str) -> str:
    return (resources.files("wdi.llm.prompts") / name).read_text(encoding="utf-8")


def _version(template: str) -> str:
    return hashlib.sha256(template.encode("utf-8")).hexdigest()[:12]


def _parse_response(text: str) -> tuple[float | None, dict[str, Any]]:
    """Parse the model's JSON answer; fall back to storing raw text."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip a ```json ... ``` fence if the model added one.
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        return None, {"raw": text}
    if not isinstance(data, dict):
        return None, {"raw": text}
    score = data.get("score")
    return (float(score) if isinstance(score, int | float) else None), data


@dataclass(frozen=True)
class Analyzer:
    key: str  # CLI-facing name
    analysis_type: str  # stored in llm_analyses
    template: str
    context_fn: Callable[[sqlite3.Connection, str], str]
    placeholder: str
    applies: Callable[[sqlite3.Connection, str], bool] = lambda _c, _s: True

    # Keep prompts well within model context and process I/O limits.
    max_context_chars: int = 40_000

    @property
    def version(self) -> str:
        return _version(self.template)

    def build_prompt(self, conn: sqlite3.Connection, session_id: str) -> str:
        ctx = self.context_fn(conn, session_id)
        if len(ctx) > self.max_context_chars:
            ctx = ctx[: self.max_context_chars] + "\n…(truncated)"
        return self.template.replace(self.placeholder, ctx)

    def parse(self, text: str) -> tuple[float | None, dict[str, Any]]:
        return _parse_response(text)


def _prompts_context(conn: sqlite3.Connection, session_id: str) -> str:
    prompts = context.user_prompts(conn, session_id)
    return "\n\n".join(f"[{i + 1}] {p}" for i, p in enumerate(prompts))


ANALYZERS: dict[str, Analyzer] = {
    "prompt-quality": Analyzer(
        key="prompt-quality",
        analysis_type="prompt_quality",
        template=_template("prompt_quality.txt"),
        context_fn=_prompts_context,
        placeholder="{prompts}",
    ),
    "summary": Analyzer(
        key="summary",
        analysis_type="session_summary",
        template=_template("session_summary.txt"),
        context_fn=context.summary_context,
        placeholder="{context}",
    ),
    "agent-efficiency": Analyzer(
        key="agent-efficiency",
        analysis_type="agent_efficiency",
        template=_template("agent_efficiency.txt"),
        context_fn=context.agent_efficiency_context,
        placeholder="{context}",
        applies=context.has_subagents,
    ),
    "workflow-recommendations": Analyzer(
        key="workflow-recommendations",
        analysis_type="workflow_recommendations",
        template=_template("workflow_recommendations.txt"),
        context_fn=context.workflow_recommendations_context,
        placeholder="{context}",
    ),
}
