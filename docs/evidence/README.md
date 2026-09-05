# WD-002 live evidence

`wd002-windows.json` contains sanitized summaries captured on 2026-09-05 by
`scripts/capture_hook.py`. These are live structural observations, not replayable
provider input fixtures. Synthetic examples remain under `tests/fixtures/hooks`.

- `codex` combines the trusted CLI turn, manual compaction/interruption/quit, and
  the subsequent native desktop task API resume of the same session.
- `claude` covers explicit-settings CLI probes, including diagnostic retries.
- `claude-desktop` covers local Code folder selection, a no-tool subagent, turn
  completion, and manual compaction with project-local settings.
- Records retain capture order within each dataset. Wall-clock timestamps were
  removed. Native ID hashes were replaced with stable aliases per field and
  dataset; aliases from different datasets must not be joined.
- Counts include repeated lifecycle callbacks and diagnostic attempts. They do
  not establish exactly-once delivery, task counts, or event completeness.
- Only allowlisted field names/types, event/tool names, and any numeric exit
  codes are retained. Unknown keys and all content are discarded. This is not a
  complete schema inventory, a transcript, or a production redaction policy.

One direct synthetic Codex no-op invocation had no identities and was excluded
from the live dataset. See [the report](../provider-compatibility.md) for versions,
the per-event matrix, reproduction steps, cleanup, and explicit limitations.
