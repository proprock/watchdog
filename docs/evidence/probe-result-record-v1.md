# Isolated probe result record v1

Every future isolated provider probe must publish one sanitized result record in
this format. It records the boundary actually exercised; it does not authorize a
provider invocation, hook installation, or access to an active provider profile.
Use a new record for every probe attempt or retry window. Do not merge a
synthetic workload, direct adapter invocation, and provider-driven observation
into one record.

`format_version` is exactly `watchdog.probe-result.v1`. A later incompatible
record uses a new format version and retains this document unchanged. Required
keys are shown below. `null` means the value is unavailable; it never means zero
or success. Identifiers, paths, commands, prompts, tool content, native IDs, and
provider account details must be replaced by stable per-record aliases or
omitted.

```json
{
  "format_version": "watchdog.probe-result.v1",
  "record_id": "wdNNN-provider-surface-YYYY-MM-DD-attempt-N",
  "classification": "synthetic | local_adapter | live_provider",
  "started_at": "2026-09-10T12:34:56Z",
  "finished_at": "2026-09-10T12:35:56Z",
  "provenance": {
    "provider": {"name": "codex | claude | other", "version": null, "surface": null},
    "harness": {"name": "codex-cli | desktop | test-script | other", "version": null},
    "adapter": {"name": "native | python | none", "version": null, "invocation": "provider | direct | simulated"},
    "os": {"name": "Windows | macOS | Linux", "version": null, "architecture": null},
    "source": {"kind": "fixture | generated-workload | manual-observation", "sanitized_reference": null}
  },
  "scope": {
    "isolation": "isolated home/profile and scratch project | test temporary directory | other",
    "expected_callback_kinds": ["sanitized event names only"],
    "expected_deliveries": 0,
    "notes": null
  },
  "stages": {
    "provider_dispatch": {
      "state": "confirmed | failed | missing | not_applicable | unknown",
      "attempts": 0,
      "evidence": null,
      "detail": null
    },
    "adapter": {
      "state": "started | failed_to_start | not_invoked | unknown",
      "starts": 0,
      "failure_category": null,
      "evidence": null
    },
    "callbacks": {
      "expected": 0,
      "observed": 0,
      "missing": 0,
      "missing_attribution": "not_applicable | attributed | unknown",
      "evidence": null
    },
    "retries": {
      "count": 0,
      "scopes": ["provider_dispatch | adapter | persistence | delivery"],
      "reason": null,
      "outcome": "not_attempted | recovered | still_failing | unknown",
      "evidence": null
    },
    "persistence": {
      "accepted": 0,
      "losses": [
        {"category": "payload | quota | invalid | io | busy | other", "count": 0, "attribution": "attributed | unknown", "detail": null}
      ],
      "evidence": null
    },
    "end_to_end_delivery": {
      "expected": 0,
      "persisted": 0,
      "read_back": 0,
      "state": "confirmed | failed | incomplete | not_applicable | unknown",
      "failure_attribution": "not_applicable | attributed | unknown",
      "failure_detail": null,
      "evidence": null
    }
  },
  "cleanup": {
    "resources": [
      {"kind": "hook_installation | process | temporary_state | task | other", "alias": "sanitized alias", "action": "removed | stopped | archived | retained", "state": "confirmed | failed | unknown", "failure_attribution": "not_applicable | attributed | unknown", "detail": null, "evidence": null}
    ],
    "complete": false,
    "remaining_state": null
  },
  "result": {"state": "passed | failed | incomplete", "limitations": ["explicitly unknown or untested facts"]}
}
```

## Recording rules

- `classification` is an evidence boundary, not a quality label. `synthetic`
  proves only the stated generated/fixture workload; `local_adapter` proves a
  direct local adapter path, not provider dispatch; only `live_provider` may
  report an observed provider callback or dispatch.
- `scope.expected_callback_kinds` names the distinct sanitized callback kinds
  in scope. `callbacks.expected`, `observed`, and `missing` are total callback
  instances across those kinds, so the three counts must reconcile as
  `expected = observed + missing` when the expected set is known.
- For `synthetic` and `local_adapter`, set `provider_dispatch.state` to
  `not_applicable`. A direct adapter process must be reported in `adapter`, not
  promoted to provider evidence. For `live_provider`, record the exact observed
  provider dispatch state; a missing callback is `missing`, not a zero-loss
  result.
- All counters are attempt-window counts. A nonzero `callbacks.missing`, failed
  adapter state, nonzero loss, failed delivery state, or failed cleanup requires
  either an attributable category/detail or the literal attribution `unknown`.
  Do not infer a cause from a successful provider command, a zero aggregate loss
  counter, or an absent log.
- `provider_dispatch.attempts` counts provider-dispatch invocations. It is zero
  for `synthetic`/`local_adapter` records (`not_applicable`) and when a live
  probe could not invoke the provider; the latter must be `failed` or `unknown`
  with evidence. `retries.count` excludes the initial attempt and names the
  repeated stage in `retries.scopes`. A retry of provider dispatch increases
  both values; an adapter-only retry does not invent another dispatch attempt.
- `persistence.losses` lists every nonzero persisted loss category. Empty is
  allowed only after the relevant counters were read and recorded in `evidence`;
  it is not a substitute for callback or delivery accounting.
- `end_to_end_delivery.state = confirmed` requires a stated expected count plus
  a read-back count from the isolated store. If a stage was not exercised, use
  `not_applicable`; if it was exercised but cannot be observed, use `unknown`.
- `cleanup.resources` names every hook installation, owned process, temporary
  state, and probe task touched by the run. Record retained state explicitly and
  set `complete` only when each owned resource has a confirmed terminal action.
- `result.state` is `passed` only when every stage required by `scope` is
  `confirmed` or `not_applicable`, all expected deliveries were read back, and
  cleanup is complete. A required `unknown`, an uncleaned owned resource, or an
  unfinished stage makes the result `incomplete`; a demonstrated failure makes
  it `failed` even when its attribution is known.

The record may reference a separate sanitized aggregate or reproducible command
description, but raw trace content remains untrusted data and must not be copied
into the record. Historical evidence predating v1 remains historical; do not
retrofit it or treat the template as a claim that an old probe met every field.
