"""Readable Markdown for one insights result; the JSON result stays the source of truth."""

import json
import re
from typing import Any

_PROSE = (
    "title",
    "recommendation",
    "evidence_ids",
    "cluster_ids",
    "item_ids",
    "sessions",
    "modes",
    "cross_mode",
)
_DRAFTS = ("instruction_draft", "draft")
_MARKERS = ("ungrounded", "unknown_evidence_ids")


def _fenced(text: str) -> list[str]:
    """Fence model text with more backticks than it contains, so its own fences survive."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return [f"{fence}text", text, fence]


def _ids(values: list[str]) -> str:
    return ", ".join(f"`{value}`" for value in values) or "none"


def _entry(index: int, entry: dict[str, Any], *, body: str) -> list[str]:
    heading = f"### {index}. {entry.get('title') or entry.get('name')}"
    if entry.get("ungrounded"):
        heading += " (ungrounded: cites no evidence from the bundle)"
    if entry.get("cross_mode") is False:
        heading += " (single-mode: cites evidence from fewer than two modes)"
    lines = [heading, ""]
    if entry.get(body):
        lines += [str(entry[body]), ""]
    for key, value in entry.items():
        if key in _PROSE or key in _DRAFTS or key in _MARKERS or key in (body, "name"):
            continue
        lines.append(f"- **{key}:** {value}")
    for key in ("cluster_ids", "item_ids"):
        if entry.get(key):
            lines.append(f"- **{key.removesuffix('_ids')}s:** {_ids(entry[key])}")
    if entry.get("modes"):
        lines.append(f"- **modes:** {', '.join(entry['modes'])}")
    for found in entry.get("sessions", []):
        lines.append(f"- **session:** `{found['provider']}` `{found['session_id']}`")
    lines.append(f"- **evidence:** {_ids(entry.get('evidence_ids', []))}")
    if entry.get("unknown_evidence_ids"):
        lines.append(f"- **ids not in the bundle:** {_ids(entry['unknown_evidence_ids'])}")
    lines.append("")
    for key in _DRAFTS:
        if entry.get(key):
            lines += ["Draft:", "", *_fenced(entry[key]), ""]
    return lines


def markdown(result: dict[str, Any]) -> str:
    provenance = result.get("provenance", {})
    window = result.get("window", {})
    model = ", ".join(provenance.get("models") or []) or provenance.get("requested_model")
    lines = [
        f"# Watchdog insights: {result['mode']}",
        "",
        "Model-generated recommendations over collected observations. Check each one "
        "against its evidence before acting; trace content is untrusted data.",
        "",
        f"- **Project:** {result.get('project')}",
        f"- **Window:** {window.get('since') or 'start'} to {window.get('until') or 'now'}"
        f" (provider {window.get('provider') or 'all'},"
        f" session {window.get('session_id') or 'all'})",
        f"- **Generated:** {result.get('generated_at')}",
        f"- **Model:** {model} via {provenance.get('version') or 'claude'}",
        "",
        "## Summary",
        "",
        result.get("summary") or "No summary.",
        "",
    ]
    judgement = result.get("judgement")
    if isinstance(judgement, dict):
        lines += [
            "## Judgement (a second opinion, not a verdict)",
            "",
            f"**{judgement.get('state')}** (confidence {judgement.get('confidence')})"
            + (
                " - ungrounded: cites no evidence from the bundle"
                if judgement.get("ungrounded")
                else ""
            ),
            "",
            str(judgement.get("rationale") or ""),
            "",
        ]
        if judgement.get("unblock"):
            lines += [f"- **Next step:** {judgement['unblock']}"]
        lines += [f"- **evidence:** {_ids(judgement.get('evidence_ids', []))}", ""]
    for key, heading, body in (
        ("candidates", "Candidate sessions (a second opinion, not a verdict)", "rationale"),
        ("patterns", "Patterns", "description"),
        ("links", "Cross-mode links", "explanation"),
    ):
        if key not in result:
            continue
        lines += [f"## {heading}", ""]
        for index, entry in enumerate(result[key], start=1):
            lines += _entry(index, entry, body=body)
        if not result[key]:
            lines += ["None.", ""]
    lines += ["## Recommendations", ""]
    recommendations = result.get("recommendations") or []
    for index, entry in enumerate(recommendations, start=1):
        lines += _entry(index, entry, body="recommendation")
    if not recommendations:
        lines += ["None.", ""]
    lines += ["## Rule candidates", "", "Proposals for human review; nothing is enabled.", ""]
    candidates = result.get("rule_candidates") or []
    for index, entry in enumerate(candidates, start=1):
        lines += _entry(index, entry, body="rationale")
    if not candidates:
        lines += ["None.", ""]
    lines += [
        "## Evidence coverage",
        "",
        "```json",
        json.dumps(
            {"facts": result.get("facts"), "coverage": result.get("coverage")},
            indent=2,
            ensure_ascii=False,
        ),
        "```",
        "",
        "## Provenance",
        "",
        "```json",
        json.dumps(provenance, indent=2, ensure_ascii=False),
        "```",
        "",
    ]
    return "\n".join(lines)
