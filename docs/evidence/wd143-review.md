# WD-143 design review

Status: cooperative-thread execution selected by the user, 2026-10-18. This record
is a design decision and discussion, not implementation or acceptance evidence.
The execution review is resolved; implementation remains open.

## Accepted decision

The user explicitly selected cooperative threads with the described limitations,
and code correction plus daemon restart as recovery after a hang. The normative
planned contract is in the [architecture decision](../architecture.md#wd-143-python-rule-execution-decision-2026-10-18).
It includes approved-byte execution, supervision of initialization and evaluation,
timeout diagnostics, rejection of late results, disabling the affected rule until
restart, and bounded workers without replacing still-running timed-out threads.
It withdraws the former hard execution deadline and daemon-immunity promises.
No pre-approval execution, TENETS change, deployment or live call was authorized.

## Process and thread overhead

A local Windows microbenchmark using Python 3.14.3 measured an empty thread's
start/join and a fresh Python process's start/exit. Each case was warmed once.
The process ran `sys.executable -I -c pass`, with standard streams redirected
to `DEVNULL`, `CREATE_NO_WINDOW`, and a five-second timeout. Measurements used
`time.perf_counter`; p95 is sorted sample index `int(0.95 * (count - 1))`.

| Operation | Samples | Median | p95 | Minimum |
|---|---:|---:|---:|---:|
| Thread start/join | 100 | 0.263 ms | 7.533 ms | 0.203 ms |
| Python process start/exit | 30 | 79.781 ms | 105.473 ms | 70.258 ms |

These are same-run startup baselines, not a rule-runner benchmark. They exclude
rule imports, input/output serialization, daemon integration, and realistic
rule evaluation. Memory overhead was not measured. A fresh process per evaluation
would incur startup repeatedly; a reusable worker could amortize startup, while
retaining process termination and replacement after a timeout. Warm-worker
latency was not measured. The user subsequently selected cooperative threads.

## Why a hung thread cannot simply be cleaned up

Python's [threading contract](https://docs.python.org/3/library/threading.html)
does not provide forced thread termination. A cancellation flag works only if
the rule cooperates; returning from a timed wait does not stop the thread.
Deleting its Python reference does not stop execution either.

Windows exposes `TerminateThread`, but its
[documented hazards](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-terminatethread)
include abandoned critical sections and heap locks and inconsistent shared
state. Forced termination inside the daemon therefore cannot satisfy the
former requirement that an arbitrary bad rule must not stop the daemon.

## Review resolution

- Cooperative threads with explicitly weaker guarantees were selected.
- The alternative process lifetime/reuse policy is not needed for this decision.
- Execution must use the approved byte snapshot; no approval invariant exception
  was requested or accepted. WD-144 subsequently selected approval before replay
  and separate enablement; see the [implemented replay workflow](../rules.md#python-rules-and-replay-wd-143).

## User operating assumptions and recovery discussion

The user expects debugged and tested rule code, with failures diagnosed from logs,
fixed, and released as updated code. The user selected the cooperative-thread
design under this trusted-code operating model and accepted restart for recovery.
No measured hang incidence or numerical probability is available. Tests alone do
not establish that arbitrary rule execution always terminates, particularly when
it performs external I/O or waits on locks.

The accepted reduced contract is a supervisor deadline, a timeout diagnostic,
disabling the affected rule after the first timeout, and no further evaluation
of that rule until daemon restart. A bounded concurrency policy must prevent
repeated timeouts from creating an unbounded number of workers. Updating a rule
file cannot reclaim an already-hung thread; recovery requires daemon restart.
This design cannot promise that a hung rule leaves the daemon unaffected, since
threads share process state and locks. The user explicitly accepted these tradeoffs.
