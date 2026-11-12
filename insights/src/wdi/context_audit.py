"""Reproducible local context audit. Generated candidates contain local previews."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from wdi.context_analysis import ContextAnalysisFilter, build_context_report
from wdi.db.connection import connect


def audit(db: Path) -> dict[str, object]:
    conn = connect(db)
    try:
        cohorts = [
            dict(row)
            for row in conn.execute("""
            SELECT agent, model, cli_version, is_noise, COUNT(*) AS sessions,
                   MIN(started_at) AS first, MAX(ended_at) AS last
            FROM sessions GROUP BY agent, model, cli_version, is_noise
        """)
        ]
        providers = []
        candidates = []
        for agent in ("claude", "codex"):
            report = build_context_report(conn, ContextAnalysisFilter(agent=agent, top_n=100))
            coverage: Counter[str] = Counter()
            for row in conn.execute(
                """SELECT coverage, COUNT(*) AS n FROM context_events
                JOIN sessions USING(session_id) WHERE agent=? GROUP BY coverage""",
                (agent,),
            ):
                coverage[row["coverage"]] = row["n"]
            providers.append(
                {
                    "agent": agent,
                    "sessions": report.total_sessions,
                    "calls": report.total_model_calls,
                    "compactions": report.total_compactions,
                    "cohorts": [asdict(p) for p in report.providers],
                    "event_coverage": dict(coverage),
                }
            )
            for kind, query in [
                (
                    "growth",
                    """
                SELECT ce.session_id, ce.stream_id, ce.idx, ce.category, ce.tool_name,
                       ce.byte_count, ce.extra, s.agent
                FROM context_events ce JOIN sessions s USING(session_id)
                WHERE s.agent=? AND ce.event_type='tool_output'
                ORDER BY ce.byte_count DESC LIMIT 20
            """,
                ),
                (
                    "error",
                    """
                SELECT t.session_id, t.stream_id, t.idx, t.tool_name, t.is_error,
                       t.exit_code, t.input_preview, t.output_preview, s.agent
                FROM tool_calls t JOIN sessions s USING(session_id)
                WHERE s.agent=? AND t.is_error=1
                ORDER BY COALESCE(t.output_bytes,0) DESC LIMIT 20
            """,
                ),
            ]:
                candidates.extend({"kind": kind, **dict(r)} for r in conn.execute(query, (agent,)))
        return {
            "method": "context-efficiency-v1",
            "cohorts": cohorts,
            "providers": providers,
            "review_candidates": candidates,
            "usage_observations": [
                dict(r)
                for r in conn.execute(
                    """SELECT agent,event_type,COUNT(*) n FROM context_events
                    JOIN sessions USING(session_id)
                    WHERE event_type IN ('model_call','usage_observation')
                    GROUP BY agent,event_type"""
                )
            ],
        }
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.db.is_file():
        parser.error("Database does not exist; import source sessions first")
    result = audit(args.db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
