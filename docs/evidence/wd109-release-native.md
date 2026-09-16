# WD-109 release automation: evidence

Actual GitHub Actions runs against `proprock/watchdog`, not configuration read
as if it had run. `.github/workflows/release-native.yml` triggers on a
`v*` tag: `package` (locked release build per target) -> `smoke` (install the
packaged artifact with no local Rust toolchain, run
`tests/test_rust_adapter.py` against it) -> `publish` (checksums, GitHub
release). The workflow, `scripts/package_native_artifact.py`, and
`src/agent_watchdog/native_artifacts.py` predate this record (commit
`3c1c74e`, "feat(release): package native hook artifacts"); this record
covers getting a real tagged run to pass, which the branch had never
attempted before 2026-09-16 (`feature/wd-109-native-release-artifacts` had
not been pushed to `origin` until this session).

## Outcome

The declared release matrix is three targets, not the four originally
implemented. Section "Dropped: x86_64-apple-darwin" below states why.

| Target | Runner | Package | Smoke |
| --- | --- | --- | --- |
| `x86_64-pc-windows-msvc` | `windows-latest` | pass | pass |
| `x86_64-unknown-linux-gnu` | `ubuntu-latest` | pass | pass |
| `aarch64-apple-darwin` | `macos-latest` | pass | pass |

Tag `v0.1.4`, run
[35150102140](https://github.com/proprock/watchdog/actions/runs/35150102140):
`package` (3/3), `smoke` (3/3), `publish` all succeeded. Published release:
[github.com/proprock/watchdog/releases/tag/v0.1.4](https://github.com/proprock/watchdog/releases/tag/v0.1.4)
(id `390272288`, published `2026-09-16T21:04:11Z`), with four assets:

- `agent-watchdog-hook-v0.1.4-x86_64-pc-windows-msvc.zip` (196210 bytes)
- `agent-watchdog-hook-v0.1.4-x86_64-unknown-linux-gnu.zip` (274168 bytes)
- `agent-watchdog-hook-v0.1.4-aarch64-apple-darwin.zip` (245021 bytes)
- `SHA256SUMS.txt` (360 bytes)

`native_artifacts.py::target_for_host` and `docs/hooks.md` declare the same
three targets; an unsupported host (including an actual Intel Mac) raises
`ValueError: Unsupported native adapter target: ...` with the supported list,
covered by `test_unsupported_host_has_an_explicit_diagnostic`
(`tests/test_native_artifacts.py`).

## Why four tagged attempts before a green one

`v0.1.0` through `v0.1.3` each published nothing; every fix below was found
because this was the branch's first exposure to a real GitHub Actions
environment, not inferred from reading the workflow file.

### 1. `ty check` failed on Linux and macOS runners (`v0.1.0` CI, not yet the release workflow)

`ty` resolves platform-conditional stdlib attributes (`ctypes.WinDLL`,
`subprocess.CREATE_NO_WINDOW`, ...) against `--python-platform`, which
defaults to the host running it. Every prior local run was on Windows, so
this had never surfaced. Reproduced locally with
`uv run ty check --python-platform linux`: 10 diagnostics in
`scripts/codex_hook_canary.py` (`OwnedProcessJob.__init__` used
`ctypes.WinDLL`/`WinError`/`get_last_error` with no platform guard, unlike
the rest of the file) and `tests/test_daemon.py` (a `@pytest.mark.skipif`
does not narrow static analysis; the assertions needed their own
`if os.name != "nt":` guard, confirmed empirically to be what makes `ty`
narrow the block). Fixed in `132b131`.

### 2-3. `bash` resolved to the WSL launcher stub, not Git Bash (`windows-latest` CI)

`tests/test_benchmark_hooks.py::test_hook_invocation_round_trips_harmless_arguments[bash]`
failed with garbled UTF-16 output and a nonzero exit from `bash -c ...` on
`windows-latest`. Root cause: Windows resolves an unqualified process name
(`Command::new`/`subprocess.Popen(["bash", ...])`) by checking the system
directory (`System32`, home to the legacy WSL `bash.exe` stub) *before*
walking `PATH`, regardless of `PATH` order. `shutil.which("bash")` only
walks `PATH`, so it reported a working Git Bash while the real bare-command
invocation still hit the broken stub. First attempt (`0e086a6`) probed with
the *resolved* path and still exited 0 for `bash -c "exit 0"` with no distro
installed, so it didn't filter the stub out. Second attempt (`4016459`)
probes with the same bare `"bash"` command `hook_invocation` actually
launches, and requires it to run `printf ok` and match the output — this is
what finally distinguished the stub from a real shell.

### 4. `writer_lock` open-time race left unretried (`windows-latest` CI)

`tests/test_daemon.py::test_independent_cli_startup_contenders_and_pause`
spawns three real `agent-watchdog daemon start` processes racing to become
the daemon. One race loser returned `{"error": "PermissionError"}` instead
of yielding harmlessly — not reproducible in 5 local attempts, only under
genuine concurrent OS-level contention. `writer_lock`
(`src/agent_watchdog/storage.py`) wrapped the byte-range lock call in
`except OSError: raise WriterBusy`, but not the preceding `path.open(...)`
call; a `PermissionError` from a concurrent creator or an AV scan racing
the file's creation propagated raw past `control_lock`'s
`except WriterBusy`-only retry loop. Fixed in `2ac0c28`, with a new
regression test (`test_concurrent_creation_race_at_open_is_reported_as_busy`)
alongside the existing lock-byte-contention one.

### 5. `macos-13` is fully retired from GitHub's hosted runner images

The original matrix built `x86_64-apple-darwin` on `runner: macos-13`. That
package job sat `queued` from `2026-09-16T19:07:11Z` with zero status change
for over 45 minutes (run `35138610710`). `actions/runner-images`'s current
image table no longer lists `macos-13` at all; free-tier standalone Intel
macOS runners do not exist any more (x64 macOS is only offered as the paid
`-large`/`-intel` larger-runner tier). `v0.1.0` was abandoned unpublished.

### 6-8. Cross-compiling `x86_64-apple-darwin` on `macos-latest` (`v0.1.1`-`v0.1.3`, all abandoned)

Switched to building `x86_64-apple-darwin` via `cargo build --target` on the
arm64 `macos-latest` runner (`affbaa3`), smoke-tested via Rosetta 2, with
`install_artifact`'s host detection pinned explicitly per matrix leg
(`system`/`machine`) since the runner's own `platform.machine()` reports
`arm64` even while executing the x86_64 artifact under Rosetta.

- `v0.1.1` (run
  [35146355015](https://github.com/proprock/watchdog/actions/runs/35146355015)):
  package 4/4, smoke 3/4 — `x86_64-apple-darwin` smoke failed:
  `test_copied_native_binary_starts_python_core_and_preserves_worktrees`
  missed its 15-second wait for the native-launched daemon to report
  `"running"` by ~0.09 s. Read as a timing margin problem.
- `v0.1.2` (run
  [35147153414](https://github.com/proprock/watchdog/actions/runs/35147153414)):
  bumped the wait to 30 s (`f78203d`). Failed again, missing the deadline by
  the near-identical ~0.05 s — the signature of a predicate that never
  becomes true, not one that's merely slow (a poll loop asserts right after
  crossing the deadline regardless of how far out the deadline is). The
  timing fix was a dead end, left in place as harmless extra CI margin.
- `v0.1.3` (run
  [35147883156](https://github.com/proprock/watchdog/actions/runs/35147883156)):
  diagnosed the real mechanism — a natively-built binary gets an automatic
  ad-hoc code signature from the linker; a cross-compiled binary does not,
  and `fork()` from an unsigned process under Rosetta 2 can silently never
  reach the forked child's `exec()`. `ensure_daemon()`
  (`native/src/main.rs`) forks, calls `libc::setsid()` via `pre_exec`, then
  execs the Python core with all of its stdio discarded
  (`Stdio::null()` x3), so there was no log to inspect either way. Added
  `codesign --force --sign -` on the cross-compiled binary before packaging
  (`143f116`). Smoke still failed the same way — the signing fix did not
  resolve it, and the exact mechanism was not confirmed further before the
  target was dropped.

### 9. Decision: drop `x86_64-apple-darwin`

After three unpublished tags spent on the cross-compiled Intel-macOS leg,
the target was dropped rather than debugged further: GitHub has already
retired free Intel-hosted macOS runners entirely, and Apple Silicon is
several hardware generations into replacing Intel Macs. Shipping this
target would mean permanently carrying a cross-compilation-plus-Rosetta
workaround for a shrinking install base. Removed from
`release-native.yml`'s `package`/`smoke` matrices and from
`native_artifacts.py`'s `_TARGETS` (`32568d7`); `docs/hooks.md` updated;
`Darwin`/`x86_64` now gets the same explicit unsupported-target diagnostic
as any other undeclared host.

## Full attempt history

| Tag | Run | Package | Smoke | Publish |
| --- | --- | --- | --- | --- |
| `v0.1.0` | [35138610710](https://github.com/proprock/watchdog/actions/runs/35138610710) | 3/4 (darwin-x86_64 stuck queued forever) | never ran | never ran |
| `v0.1.1` | [35146355015](https://github.com/proprock/watchdog/actions/runs/35146355015) | 4/4 | 3/4 | skipped |
| `v0.1.2` | [35147153414](https://github.com/proprock/watchdog/actions/runs/35147153414) | 4/4 | 3/4 | skipped |
| `v0.1.3` | [35147883156](https://github.com/proprock/watchdog/actions/runs/35147883156) | 4/4 | 3/4 | skipped |
| `v0.1.4` | [35150102140](https://github.com/proprock/watchdog/actions/runs/35150102140) | 3/3 | 3/3 | **succeeded** |

None of `v0.1.0`-`v0.1.3` produced a GitHub release; `v0.1.4` is the only
published one.

## Companion CI evidence

The general `check` matrix (`.github/workflows/ci.yml`) also ran for the
first time on this branch in this session and needed the same ty/bash/
writer_lock fixes above; its first fully green run after all fixes was
[35143147468](https://github.com/proprock/watchdog/actions/runs/35143147468)
(`ubuntu-latest`/3.12, `ubuntu-latest`/3.14, `macos-latest`/3.12,
`windows-latest`/3.12, all pass). The extra Python coverage leg was also
bumped from 3.13 to 3.14 (`a607f41`), per request.
