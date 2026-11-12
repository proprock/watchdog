#!/usr/bin/env bash
#
# Runs the weekly agent-efficiency review on Linux.
#
# Cron example:
# 0 9 * * 1 <repo>/scripts/weekly-review.sh
#
# Notes:
# - The analyze steps require the claude CLI to be available for the configured claude-cli backend.
# - The task must run as the user who can read local Claude/Codex session files.
# - Review reports are written under reports/, which is intentionally git-ignored.
# - Override the uv cache with WDI_UV_CACHE_DIR=/path/to/cache if needed.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
REPORT_DIR="${WDI_REVIEW_REPORT_DIR:-$REPO_ROOT/reports}"
STAMP="$(date +%F-%H%M%S)"
REPORT_PATH="$REPORT_DIR/weekly-review-$STAMP.md"
UV_CACHE_DIR="${WDI_UV_CACHE_DIR:-/tmp/uv-cache}"

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin:${PATH:-}"

format_command_line() {
    printf 'uv'
    printf ' %q' "$@"
}

run_review_step() {
    local name="$1"
    shift

    local started
    local finished
    local duration
    local status
    local output
    local command_line

    started="$(date +%s)"
    command_line="$(format_command_line --cache-dir "$UV_CACHE_DIR" run "$@")"

    {
        printf '## %s\n\n' "$name"
        printf 'Command: `%s`\n\n' "$command_line"
    } >>"$REPORT_PATH"

    set +e
    output="$(
        cd "$REPO_ROOT" &&
            uv --cache-dir "$UV_CACHE_DIR" run "$@" 2>&1
    )"
    status=$?
    set -e

    finished="$(date +%s)"
    duration=$((finished - started))

    {
        printf 'Exit code: %s\n' "$status"
        printf 'Duration: %s seconds\n\n' "$duration"
        printf '```text\n'
        printf '%s\n' "$output"
        printf '```\n\n'
    } >>"$REPORT_PATH"

    return "$status"
}

mkdir -p "$REPORT_DIR"

{
    printf '# Weekly agent-efficiency review\n\n'
    printf 'Generated: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')"
    printf 'Repository: %s\n\n' "$REPO_ROOT"
} >"$REPORT_PATH"

if run_review_step "Import sessions" wdi import &&
    run_review_step "Stats" wdi stats &&
    run_review_step "Prompt quality analysis" \
        wdi analyze --type prompt-quality --limit 10 --yes &&
    run_review_step "Agent efficiency analysis" \
        wdi analyze --type agent-efficiency --limit 10 --yes; then
    {
        printf '## Result\n\n'
        printf 'All review steps completed.\n'
    } >>"$REPORT_PATH"
    printf 'Weekly review report written to %s\n' "$REPORT_PATH"
else
    status=$?
    {
        printf '## Result\n\n'
        printf 'Review failed with exit code %s.\n' "$status"
    } >>"$REPORT_PATH"
    printf 'Weekly review failed. Report written to %s\n' "$REPORT_PATH" >&2
    exit "$status"
fi
