## Mandatory rules

- Strictly follow `TENETS.md`.

## Delegation and context budget

- Keep one primary agent responsible for scope, planning, integration, acceptance criteria, and final review. Delegate substantial, bounded work that can proceed independently alongside useful primary-agent work; keep tiny edits, tightly coupled steps, and coordination-heavy work local, because delegation has its own cost. Model routing for a particular orchestration setup is host-local and lives in the ignored `LIVE.md`.
- Start delegated work with a fresh, minimal context. Supply the objective, exact file ownership, relevant interfaces and invariants, acceptance checks, and stop conditions, and point the worker at the applicable repository instructions. Do not copy the whole conversation, transcripts, or unrelated tool definitions.
- Batch related mechanical edits into one assignment. Parallelize only independent work with disjoint write ownership. Do not launch speculative reviewers, duplicate investigations, or nested delegation by default. Workers do not commit, push, or launch live provider probes unless explicitly assigned those actions within existing authorization.
- Require a compact handoff: changes made, relevant file or symbol references, checks and results, and unresolved issues. A worker stops and reports when the task exceeds its contract or needs assumptions outside it. Allow a focused correction when new evidence or a clearly identified defect justifies it; do not retry the same worker without new information.
- The primary agent inspects the diff and supporting evidence before accepting a handoff and runs the local checks in `TENETS.md`. Delegation does not replace review or expand authorization.
- Filter tool output before returning it to model context. Prefer symbol snippets, bounded log windows, counts, structured summaries, and file references. Parse large history, MCP, or JSON results locally and return only relevant fields; avoid whole-task dumps and recursive directory listings. Set explicit output limits and narrow queries when results truncate.
- Read relevant instructions once per context and reuse verified findings. Batch independent reads and avoid status polling unless the result can affect a decision. At natural phase boundaries, prefer a compact handoff into a fresh context over carrying an inflated investigation history.
- Evaluate delegation using aggregate input, cached input, output, elapsed time, and rework, and treat cheaper delegation as a hypothesis to measure, not guaranteed savings.

## Tests

- Local test selection and the full-suite rule are in `TENETS.md`. Do not alter the full cross-platform `uv run pytest` run in `.github/workflows/ci.yml`.
