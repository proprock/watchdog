# Verification evidence

`wd024-windows-baseline.json` preserves the original direct Rust measurement.
`wd024-windows-direct-recheck.json`, `wd024-windows-powershell.json`, and
`wd024-windows-pwsh.json` compare direct and shell launches of the installed Rust
binary. All are synthetic 81-event workloads, with a 15-second process timeout;
they are not native hook-runner deadline tests. The recheck/shell runs use a
one-second requested idle interval, so the original 20-second baseline remains
the meaningful idle-cost observation. See [verification](../verification.md)
for the first-call latency, native event gaps, and open WD-024 acceptance.

`wd024-windows-native.json` contains sanitized aggregate counts from the two
isolated native coding sessions, with separate recovery/concurrent windows. It
excludes synthetic shell-timing events and contains no transcripts or native IDs.
Aggregate loss counters are not a complete native callback delivery audit.

`wd008-windows-baseline.json` is a separate **synthetic** hook-process and idle
benchmark, not native provider evidence. See [CLI measurement](../cli.md) for its
workload, commands, and the unmet latency target assigned to WD-024.

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
