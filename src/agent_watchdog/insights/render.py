"""Readable Markdown for one insights result; the JSON result stays the source of truth."""

import json
from typing import Any

_PROSE = ("title", "recommendation", "instruction_draft", "evidence_ids", "cluster_ids")
_MARKERS = ("ungrounded", "unknown_evidence_ids")


def _ids(values: list[str]) -> str:
    return ", ".join(f"`{value}`" for value in values) or "none"


def _entry(index: int, entry: dict[str, Any], *, body: str) -> list[str]:
    heading = f"### {index}. {entry.get('title') or entry.get('name')}"
    if entry.get("ungrounded"):
        heading += " (ungrounded: cites no evidence from the bundle)"
    lines = [heading, ""]
    if entry.get(body):
        lines += [str(entry[body]), ""]
    for key, value in entry.items():
        if key in _PROSE or key in _MARKERS or key in (body, "name"):
            continue
        lines.append(f"- **{key}:** {value}")
    if entry.get("cluster_ids"):
        lines.append(f"- **clusters:** {_ids(entry['cluster_ids'])}")
    lines.append(f"- **evidence:** {_ids(entry.get('evidence_ids', []))}")
    if entry.get("unknown_evidence_ids"):
        lines.append(f"- **ids not in the bundle:** {_ids(entry['unknown_evidence_ids'])}")
    lines.append("")
    if entry.get("instruction_draft"):
        lines += ["Instruction draft:", "", "```text", entry["instruction_draft"], "```", ""]
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
        "## Recommendations",
        "",
    ]
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
