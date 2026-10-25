"""WD-151 operator-driven Codex desktop probe through the production native adapter.

``prepare`` builds one scratch Git checkout, an isolated Watchdog installation and a
project hook that calls the production adapter directly. ``serve`` runs the scratch
decision channel while the operator drives the desktop app. ``collect`` reads the
scratch Store back and writes one sanitized record. No command launches the provider,
changes user-level configuration or touches an installed Watchdog instance.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import tomli_w
from codex_production_probe import adapter_version, paths_at

from agent_watchdog.config import Rules, UserPaths, load_config, save_config
from agent_watchdog.daemon import _drain_spool, _policy_server, _SessionTracker, mutate_registry
from agent_watchdog.storage import Store, WriterBusy, writer_lock

APP_VERSION = "26.1016.62342"
BACKEND = "0.163.0-alpha.11.2"
PROFILE = f"windows-desktop-{APP_VERSION}-{BACKEND}"
BACKEND_VERSION = re.compile(r'"cli_version"\s*:\s*"([^"]{1,64})"')
RULE_NAME = "codex_desktop_probe"
RULE_FILE = f"{RULE_NAME}.toml"
SENTINEL = "wd151-sentinel.txt"
SIDE_EFFECT = "WD151_SIDE_EFFECT"
REASON = "WD151_DENY"
DISABLED = "hooks.json.wd151-disabled"
LOCK = "daemon.lock"
MANIFEST = "manifest.json"
VERSION = re.compile(r"\d+\.\d+\.\d+")
REPOSITORY = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Case:
    name: str
    event: str
    action: str
    with_profile: bool
    # blocked: a delivered control and no side effect; approved: a delivered permit and the
    # side effect; silent: no delivered control and the side effect.
    expect: str
    escalate: bool


CASES = {
    case.name: case
    for case in (
        Case("bash-deny", "PreToolUse", "deny", True, "blocked", False),
        Case("permission-deny", "PermissionRequest", "deny", True, "blocked", True),
        Case("permission-permit", "PermissionRequest", "permit", True, "approved", True),
        Case("no-profile-deny", "PreToolUse", "deny", False, "silent", False),
    )
}


@dataclass(frozen=True)
class Target:
    """One app/backend pair and the cases that make sense for its profile."""

    app_version: str
    backend: str
    cases: tuple[str, ...]

    @property
    def profile(self) -> str:
        return f"windows-desktop-{self.app_version}-{self.backend}"


TARGETS = {
    "probed": Target(APP_VERSION, BACKEND, tuple(CASES)),
    # The profile is provisional: it carries no `permit`, so that case does not apply.
    "provisional": Target(
        "26.1021.31345",
        "0.164.0-alpha.11.1",
        ("bash-deny", "permission-deny", "no-profile-deny"),
    ),
}


@dataclass(frozen=True)
class Facts:
    callbacks: int
    controls: int
    sentinel: bool
    dialog_seen: bool | None
    app_version: str
    backend_version: str | None


def now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hook_handler(adapter: Path, paths: UserPaths, spec: Case, profile: str) -> dict[str, Any]:
    arguments = [
        str(adapter),
        "--python",
        sys.executable,
        "--config",
        str(paths.config),
        "--data",
        str(paths.data),
        "--runtime",
        str(paths.runtime),
        *(["--codex-control-profile", profile] if spec.with_profile else []),
        "hook",
        "codex",
    ]
    return {
        "type": "command",
        "command": shlex.join(arguments),
        "commandWindows": "& "
        + " ".join("'" + value.replace("'", "''") + "'" for value in arguments),
        "timeout": 30,
    }


def _rule(spec: Case) -> dict[str, Any]:
    # The escalated permit must match the whole command the desktop submits, wrapper
    # included, so only the scratch sentinel name is pinned. The rule exists in the
    # scratch installation alone.
    pattern = re.escape(SENTINEL)
    return {
        "schema_version": 1,
        "name": RULE_NAME,
        "version": "1",
        "origin": "user",
        "provider": ["codex"],
        "event": spec.event,
        "match": {
            "tool_name": "Bash",
            "input_regex": f"(?s).*{pattern}.*" if spec.action == "permit" else pattern,
        },
        "action": {"kind": spec.action, "message": REASON},
    }


def _prompt(spec: Case) -> str:
    text = (
        "Run exactly this PowerShell shell command once and report its output verbatim: "
        f"Set-Content -Path {SENTINEL} -Value {SIDE_EFFECT}. "
        "Do not retry, use another tool, or work around a refusal."
    )
    if spec.escalate:
        text += " Explicitly request sandbox_permissions=require_escalated for this scratch test."
    return text


def prepare(scratch: Path, case_name: str, adapter: Path, target: str = "probed") -> dict[str, Any]:
    scratch = scratch.absolute()
    if scratch == REPOSITORY or REPOSITORY in scratch.parents:
        raise ValueError("scratch must be outside the Watchdog repository")
    if scratch.exists() and (not scratch.is_dir() or any(scratch.iterdir())):
        raise ValueError("scratch directory must be new or empty")
    pair = TARGETS[target]
    if case_name not in pair.cases:
        raise ValueError(f"case {case_name} is not available for the {target} target")
    spec = CASES[case_name]
    banner = adapter_version(adapter)
    work = scratch / "work"
    work.mkdir(parents=True)
    subprocess.run(
        ["git", "init", "--quiet", str(work)],
        check=True,
        capture_output=True,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    paths = paths_at(scratch)
    for directory in (paths.config.parent, paths.data, paths.runtime):
        directory.mkdir(parents=True, exist_ok=True)
    mutate_registry(paths, lambda registry: registry.add(work))
    rule_path = paths.config.parent / "rules" / RULE_FILE
    rule_path.parent.mkdir(exist_ok=True)
    rule_path.write_text(tomli_w.dumps(_rule(spec)), encoding="utf-8", newline="\n")
    config = load_config(paths.config)
    approved = {RULE_NAME: digest(rule_path)}
    save_config(paths.config, config.model_copy(update={"rules": Rules(approved=approved)}))
    handler = _hook_handler(adapter, paths, spec, pair.profile)
    hooks = {
        event: [{"matcher": "^Bash$", "hooks": [handler]}]
        for event in ("PreToolUse", "PermissionRequest")
    }
    hook = work / ".codex" / "hooks.json"
    hook.parent.mkdir()
    hook.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8", newline="\n")
    (scratch / MANIFEST).write_text(
        json.dumps(
            {
                "case": case_name,
                "target": target,
                "run_id": uuid4().hex,
                "started_at": now(),
                "hook_sha256": digest(hook),
                "adapter": banner,
            }
        ),
        encoding="utf-8",
    )
    steps = [
        f"Run `serve --root {scratch}` in a terminal and leave it running.",
        f"Open {work} in Codex desktop and record the app version from About "
        f"(this target expects {pair.app_version}).",
        "Review and trust only this project's exact hook definition in the native hook UI.",
        "Start a fresh chat in this folder after trust and send the supplied prompt once.",
        "Wait for the turn to finish. Do not retry in this scratch folder.",
        "Stop `serve` (press Enter in its terminal), then run `collect`.",
    ]
    if spec.expect == "approved":
        steps.insert(4, "Note whether the desktop showed any approval dialog for the command.")
    return {"dir": str(work), "case": case_name, "prompt": _prompt(spec), "steps": steps}


def serve(scratch: Path, timeout: float = 1800) -> None:
    paths = paths_at(scratch.absolute())
    done = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.readline(), done.set()), daemon=True).start()
    with writer_lock(paths.data / LOCK), _policy_server(paths, _SessionTracker()):
        print("Decision channel ready. Press Enter to stop.", flush=True)
        done.wait(timeout)


def confirmed_controls(events: list[Any], callback_ids: set[str], action: str | None) -> int:
    """Count approved, delivered daemon controls linked to this probe's native callbacks."""
    total = 0
    for event in events:
        payload = event.payload.get("codex") if isinstance(event.payload, dict) else None
        if not (
            event.kind == "control"
            and event.provider == "codex"
            and event.source == "daemon"
            and isinstance(payload, dict)
            and payload.get("rule") == RULE_NAME
            and (payload.get("action") == action if action else payload.get("action") != "log")
            and isinstance(payload.get("delivered_at"), str)
            and payload["delivered_at"]
            and isinstance(payload.get("evidence_ids"), list)
        ):
            continue
        total += any(item in callback_ids for item in payload["evidence_ids"])
    return total


def evaluate(case_name: str, facts: Facts, target: str = "probed") -> tuple[str, list[str]]:
    """A cell is supported only with both the stored control and the provider effect."""
    spec = CASES[case_name]
    pair = TARGETS[target]
    if facts.app_version != pair.app_version:
        return "inconclusive", [f"app version differs from the target {pair.app_version}"]
    if facts.backend_version != pair.backend:
        return "inconclusive", [
            f"backend version {facts.backend_version or 'unknown'} is not the target {pair.backend}"
        ]
    if facts.callbacks == 0:
        return "inconclusive", ["no matching native callback was spooled"]
    if spec.expect == "silent":
        if facts.controls:
            return "unsupported", ["a control was delivered without the profile"]
        if not facts.sentinel:
            return "inconclusive", ["the command did not run, so silence is not shown"]
        return "supported", []
    if facts.controls == 0:
        return "inconclusive", ["no delivered control is stored for the callback"]
    if spec.expect == "blocked":
        if facts.sentinel:
            return "unsupported", ["a control was stored but the side effect occurred"]
        return "supported", []
    if facts.dialog_seen is not False:
        return "inconclusive", ["the operator must report that no approval dialog appeared"]
    if not facts.sentinel:
        return "unsupported", ["a permit was stored but the command did not run"]
    return "supported", []


def _disable_hook(work: Path, expected: str, problems: list[str]) -> None:
    hook = work / ".codex" / "hooks.json"
    disabled = work / ".codex" / DISABLED
    if hook.exists() and digest(hook) == expected and not disabled.exists():
        hook.rename(disabled)
    elif not hook.exists() and disabled.exists() and digest(disabled) == expected:
        return
    else:
        problems.append("Owned hook definition changed or could not be disabled; inspect it.")


def _backend_version(transcripts: set[str]) -> str | None:
    """The backend version recorded in the session files; None unless exactly one is seen.

    The transcript path comes from the hook payload, so it is untrusted data: only a
    bounded prefix of an existing `.jsonl` file is read and nothing in it is executed.
    """
    seen: set[str] = set()
    for name in transcripts:
        path = Path(name)
        if not path.is_absolute() or path.suffix != ".jsonl":
            continue
        try:
            with path.open("rb") as stream:
                head = stream.read(65536).decode("utf-8", errors="replace")
        except OSError:
            continue
        if found := BACKEND_VERSION.search(head):
            seen.add(found.group(1))
    return seen.pop() if len(seen) == 1 else None


def _spooled_callbacks(paths: UserPaths, spec: Case) -> tuple[set[str], set[str]]:
    found: set[str] = set()
    transcripts: set[str] = set()
    for path in (paths.data / "spool").glob("*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        hook = item.get("input") if isinstance(item, dict) else None
        if (
            isinstance(hook, dict)
            and item.get("provider") == "codex"
            and hook.get("hook_event_name") == spec.event
            and hook.get("tool_name") == "Bash"
            and isinstance(item.get("event_id"), str)
        ):
            found.add(item["event_id"])
            if isinstance(hook.get("transcript_path"), str):
                transcripts.add(hook["transcript_path"])
    return found, transcripts


def collect(scratch: Path, *, desktop_version: str, dialog_seen: bool | None) -> dict[str, Any]:
    if not VERSION.fullmatch(desktop_version):
        raise ValueError("record the exact numeric desktop app version from About")
    scratch = scratch.absolute()
    manifest = json.loads((scratch / MANIFEST).read_text(encoding="utf-8"))
    spec = CASES[manifest["case"]]
    target = manifest.get("target", "probed")
    pair = TARGETS[target]
    paths = paths_at(scratch)
    try:
        with writer_lock(paths.data / LOCK):
            pass
    except WriterBusy:
        raise ValueError("stop `serve` before collecting") from None
    problems: list[str] = []
    work = scratch / "work"
    _disable_hook(work, manifest["hook_sha256"], problems)
    callbacks, transcripts = _spooled_callbacks(paths, spec)
    config = load_config(paths.config)
    _drain_spool(paths, config)
    project = config.projects[0]
    with Store(paths.project_data(project.id), project.id) as store:
        events = store.events(limit=1000)
    controls = confirmed_controls(
        events, callbacks, None if spec.expect == "silent" else spec.action
    )
    facts = Facts(
        callbacks=len(callbacks),
        controls=controls,
        sentinel=(work / SENTINEL).exists(),
        dialog_seen=dialog_seen,
        app_version=desktop_version,
        backend_version=_backend_version(transcripts),
    )
    verdict, reasons = evaluate(manifest["case"], facts, target)
    if problems:
        verdict = "inconclusive"
    return {
        "schema": "wd151-codex-desktop-production-v1",
        "case": spec.name,
        "verdict": verdict,
        "reasons": reasons,
        "problems": problems,
        "target": target,
        "profile": pair.profile if spec.with_profile else None,
        "app_version": desktop_version,
        "backend_version": facts.backend_version,
        "adapter": manifest["adapter"],
        "observed": {
            "callbacks": facts.callbacks,
            "controls": facts.controls,
            "sentinel": facts.sentinel,
            "dialog_seen": dialog_seen,
        },
        "started_at": manifest["started_at"],
        "finished_at": now(),
        "limitations": [
            "One Windows desktop app and backend pair; the backend is read from session files.",
            "Operator-driven; trust records, chat and scratch are retained for review.",
            "Scratch decision channel run in-process; no detached daemon cadence is measured.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--root", type=Path, required=True)
    prep.add_argument("--case", choices=sorted(CASES), required=True)
    prep.add_argument("--adapter", type=Path, required=True)
    prep.add_argument("--target", choices=sorted(TARGETS), default="probed")
    run = commands.add_parser("serve")
    run.add_argument("--root", type=Path, required=True)
    coll = commands.add_parser("collect")
    coll.add_argument("--root", type=Path, required=True)
    coll.add_argument("--desktop-version", required=True)
    coll.add_argument("--dialog-seen", choices=("yes", "no"))
    coll.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "prepare":
        print(json.dumps(prepare(args.root, args.case, args.adapter, args.target), indent=2))
    elif args.action == "serve":
        serve(args.root)
    else:
        record = collect(
            args.root,
            desktop_version=args.desktop_version,
            dialog_seen=None if args.dialog_seen is None else args.dialog_seen == "yes",
        )
        args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(json.dumps({"verdict": record["verdict"], "reasons": record["reasons"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
