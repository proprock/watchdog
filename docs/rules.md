# Rules

A rule tells the daemon what to do when a hook call matches. The daemon answers
through the [decision channel](daemon.md); the adapter renders the answer for the
provider ([hooks.md](hooks.md#adapter-behavior)). WD-142 added **declarative
rules**: TOML data with a closed set of predicates and actions. They never execute
code. Python rules are planned separately (WD-143); this document will grow with
them.

Every action a rule causes is recorded as a `control` event and shows up as a
finding with `rule`, `rule_version`, `action`, `evidence_ids`, `delivered_at` and
the text sent to the model. A hook returns an action only for a rule that is
approved and enabled, on a provider and event whose control capability was
confirmed ([provider-compatibility.md](provider-compatibility.md)), and every
failure on the way is fail-open: the harness proceeds as if Watchdog were absent.

## Status and approval

A rule's status is derived, never read from its file, so a file cannot approve
itself.

| Status | Meaning | Applied |
|---|---|---|
| `approved` | A built-in, or a user rule whose current SHA-256 equals the digest in `[rules] approved` | yes |
| `proposed` | A user rule nobody approved | no |
| `stale` | A user rule approved earlier whose file has changed since | no |
| `disabled` | Named in `[rules] disabled`, or a built-in that ships off and is not in `[rules] enabled` | no |
| `invalid` | Does not parse, fails validation, is misnamed, or shadows a built-in | no |

`agent-watchdog rules approve NAME` validates the rule, prints the exact bytes and
their SHA-256, asks for `yes` (or takes `--yes`), and records the digest of the
bytes it showed. If the file changed between showing and recording it refuses, and
an invalid rule is never approved. Approval is the user's decision: a rule
cannot do more than the closed action set below, but a wrong rule can still block
a stop or ask about harmless commands. `reject` revokes the approval and keeps the
rule off; `enable` never approves.

Built-ins ship with the package and are trusted as the code is. A user rule file
must be named `<name>.toml` with the same `name` inside, and may not reuse a
built-in's name. The rules directory is `<config-dir>/rules/` (next to
`config.toml`); only `*.toml` files directly in it are read, at most 128, each at
most 64 KiB. The daemon notices a new, edited or approved rule at its next poll
tick and republishes its subscriptions; no restart is needed.

## File format

```toml
schema_version = 1
name = "my_rule"            # [a-z][a-z0-9_]{0,63}; equals the file name
version = "1"               # recorded on every finding
origin = "user"             # free text: who or what wrote it
description = ""
provider = ["claude"]       # only "claude" is rendered today (WD-151 for Codex)
event = "PreToolUse"        # PreToolUse PostToolUse PostToolUseFailure UserPromptSubmit
                            # Stop SubagentStop SessionStart PreCompact
priority = 100              # user rules run in ascending order, after the built-ins
default_enabled = true      # built-ins only; false ships the rule off

[match]
tool_name = "Bash"          # exact; omit to match every tool
input_regex = 'rm\s+-rf'    # on tool_input["command"], else on the input as compact JSON
error_regex = ""            # on the `error` text of PostToolUseFailure

[state]                     # predicates over the calling agent's session state
failures_in_turn = { min = 2 }

[action]
kind = "ask"                # log | context | ask | rewrite | deny | block
message = "Watchdog: confirm this."
cooldown_seconds = 0        # at most once per rule, session and agent per cooldown
max_per_turn = 1            # at most this many per turn (an unknown turn is one bucket)
expires_at = ""             # ISO 8601 with a UTC offset; empty never expires
```

Unknown keys, unknown predicates and unknown placeholders are errors at load, never
ignored.

### Predicates

All listed predicates must hold. A missing session state, an unobserved model or an
unparsable timestamp makes a predicate false: unknown is not zero.

| Predicate | Holds when |
|---|---|
| `repeats_same_input_output = {min,max}` | The latest calls repeat the same tool, input hash, output hash and outcome that many times in a row (0 repeats counts as unknown) |
| `edits_since_last_call = {min,max}` | Edit-class calls finished since the last other call |
| `failures_in_turn = {min,max}` | Failed tool calls in the current turn |
| `compactions_in_turn = {min,max}` | Compactions in the current turn |
| `turn_minutes = {min,max}` | Minutes since the turn started |
| `edited_without_verification = true` | Files were edited this turn and no test runner finished after the latest edit |
| `subagent_same_tier_as_coordinator = true` | The `Agent` call's `tool_input.model` is in the same family as the coordinator's observed model and above the lowest tier |

"Test runner" is a fixed list (`pytest`, `cargo test`, `npm test`, `go test`); a
shell call whose command was not captured counts as possibly one.
`[rules] tiers` ranks model families lowest first (default `haiku`, `sonnet`,
`opus`, `fable`).

### Actions

`log` records a finding and tells the adapter nothing. `context` injects the
message as context, `ask` asks the user before the tool runs, `deny` refuses it
(with the message as the reason), `rewrite` replaces fields of the tool input, and
`block` stops a `Stop`/`SubagentStop` or a `PostToolUse` result. A rule may only
name an action the adapter renders on its event:

| Event | Actions |
|---|---|
| `PreToolUse` | `deny`, `ask`, `rewrite`, `context` |
| `PostToolUse` | `block`, `context` |
| `UserPromptSubmit`, `SessionStart`, `PreCompact` | `context` |
| `Stop`, `SubagentStop` | `block` |
| `PostToolUseFailure` | `log` only |

`rewrite = { "tool_input.model" = "$lower_tier" }` sets fields under `tool_input`;
the daemon sends the whole original input with those fields changed, because the
provider replaces the input rather than merging. `$lower_tier` is the next lower
entry of `[rules] tiers`; if it cannot be resolved the rule does not fire.
Messages may use `{repeats}`, `{failures}`, `{coordinator_model}`,
`{candidate_model}` and `{lower_tier}`; an unknown value prints `unknown`.

A rewrite is rendered as `permissionDecision: allow`, so a user rule that rewrites
a `Bash` call also approves it.

### Limits

Regular expressions are at most 512 characters, may not nest a quantifier inside a
quantified group (`(a+)+`), and run on at most the first 64 KiB of input. `re`
has no timeout, so these bounds are the protection.

## Built-in rules

| Rule | Event | Action | Notes |
|---|---|---|---|
| `subagent_same_model` | `PreToolUse` on `Agent` | `rewrite` | The subagent's model moves one tier down. Reads the coordinator model from session state, so a spawn made by a subagent, whose own state has none, is not evaluated. Evidenced on Claude CLI; not evaluated on Claude desktop |
| `destructive_command` | `PreToolUse` on `Bash` | `ask` | `rm -r`/`-rf`/`--recursive` (not `git rm`), `git reset --hard`, `git push --force`/`-f`/`--force-with-lease`, `git checkout -- .` (the whole tree, not one path), `git clean -f…`, and `rm`/`del`/`erase` of an `.env` file (not `.env.example`, `.sample`, `.template`). The match is textual, so a quoted `rm -rf` in an `echo` asks too |
| `repeat_same_input_same_output` | `PostToolUse` | `context` | Three identical calls in a row; cooldown 600 s, once per turn. The count is of calls the daemon has folded in, so it can trail the call in flight by one |
| `stop_without_verification` | `Stop` | `block` | **Off by default** (`rules enable stop_without_verification`). Once per turn, and never while `stop_hook_active`. Inert when `capture_content = false`, since a command that was not captured may have been the test run. After a very recent test run the daemon may not have folded it in yet, so the block can be wrong for a moment |

## Kill switches

Two independent switches stop every action, and neither can undo the other:
`[defaults] policy_intervene = false` (global) and
`[projects.overrides] policy_intervene = false` (one project). With the global
switch off the daemon publishes no subscriptions, so adapters stop asking.
`policy_intervene_same_model_subagent_spawn` remains and turns off
`subagent_same_model` alone. `daemon pause` stops the channel altogether.

## Commands

```
agent-watchdog rules list
agent-watchdog rules show NAME
agent-watchdog rules approve NAME [--yes]
agent-watchdog rules reject NAME
agent-watchdog rules disable NAME
agent-watchdog rules enable NAME
agent-watchdog rules stats [--since ISO8601]
```

`list` shows every rule with its status and how often it fired; `stats` adds, per
rule, the firings by action, the verdicts recorded for them and the precision
(`true_positive / (true_positive + false_positive)`, `null` while nothing is
reviewed). Verdicts use the ordinary `verdict` command or
`scripts/calibrate.py annotate`: a control finding's `rule_version` is the rule's
`version`, and its evidence is the control event plus the hook event it answered.
Control findings carry an `action` and are reported apart from the M2/M3 shadow
rules; they never enter the precision gate.

## Not implemented here

Python rules and replay (`rules test`, WD-143), model-written rule candidates
(`rules propose`, WD-144), promotion between actions (WD-147) and Codex rendering
(WD-151).
