"""Opt-in Windows Codex CLI hook-launch canary; never run from the offline suite."""

import argparse
import ctypes
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import TextIO
from uuid import uuid4

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", ctypes.c_uint32),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority_class", ctypes.c_uint32),
        ("scheduling_class", ctypes.c_uint32),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("read_operation_count", ctypes.c_ulonglong),
        ("write_operation_count", ctypes.c_ulonglong),
        ("other_operation_count", ctypes.c_ulonglong),
        ("read_transfer_count", ctypes.c_ulonglong),
        ("write_transfer_count", ctypes.c_ulonglong),
        ("other_transfer_count", ctypes.c_ulonglong),
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _BasicLimitInformation),
        ("io_info", _IoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _ProcessEntry32(ctypes.Structure):
    _fields_ = [
        ("size", ctypes.c_uint32),
        ("usage", ctypes.c_uint32),
        ("process_id", ctypes.c_uint32),
        ("default_heap_id", ctypes.c_size_t),
        ("module_id", ctypes.c_uint32),
        ("threads", ctypes.c_uint32),
        ("parent_process_id", ctypes.c_uint32),
        ("priority_class_base", ctypes.c_long),
        ("flags", ctypes.c_uint32),
        ("executable", ctypes.c_wchar * MAX_PATH),
    ]


class OwnedProcessJob:
    """Windows job whose closure kills all processes belonging to this probe."""

    def __init__(self, process: subprocess.Popen) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = ctypes.c_int
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._kernel32 = kernel32
        self._handle = handle
        try:
            limits = _ExtendedLimitInformation()
            limits.basic_limit_information.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            process_handle = getattr(process, "_handle", None)
            if process_handle is None:
                raise RuntimeError("could not obtain the canary process handle")
            if not kernel32.AssignProcessToJobObject(handle, process_handle):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._handle:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def process_chain(pid: int | None = None) -> list[str]:
    """Return executable names from this handler to its surviving ancestors."""
    if os.name != "nt":
        raise RuntimeError("the Codex command-hook canary currently requires Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
    kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32)]
    kernel32.Process32FirstW.restype = ctypes.c_int
    kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ProcessEntry32)]
    kernel32.Process32NextW.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entries: dict[int, tuple[int, str]] = {}
    try:
        entry = _ProcessEntry32()
        entry.size = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            raise ctypes.WinError(ctypes.get_last_error())
        while True:
            entries[entry.process_id] = (entry.parent_process_id, entry.executable.casefold())
            entry.size = ctypes.sizeof(entry)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    finally:
        kernel32.CloseHandle(snapshot)
    current = os.getpid() if pid is None else pid
    names: list[str] = []
    for _ in range(8):
        item = entries.get(current)
        if item is None:
            break
        parent, name = item
        names.append(name)
        if parent == 0:
            break
        current = parent
    return names


def write_marker(marker: Path, event: str, chain: list[str]) -> None:
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = marker.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"schema_version": 1, "event": event, "process_chain": chain}) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.replace(marker)


def write_failure_marker(marker: Path, reason: str) -> None:
    failure = marker.with_suffix(".failure.json")
    failure.write_text(json.dumps({"schema_version": 1, "reason": reason}) + "\n", encoding="utf-8")


def handle(marker: Path, stream: TextIO) -> None:
    """Emit a content-free marker for one SessionStart callback."""
    try:
        payload = json.loads(stream.read(1024 * 1024 + 1))
        if not isinstance(payload, dict):
            write_failure_marker(marker, "input is not a JSON object")
            return
        event = payload.get("hook_event_name")
        if event != "SessionStart":
            write_failure_marker(marker, "unexpected hook event")
            return
        write_marker(marker, event, process_chain())
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as error:
        write_failure_marker(marker, f"handler error: {type(error).__name__}")


def validate_marker(marker: dict, expected_chain: list[str]) -> None:
    if marker.get("schema_version") != 1 or marker.get("event") != "SessionStart":
        raise RuntimeError("unexpected canary event")
    if marker.get("process_chain") != expected_chain:
        raise RuntimeError("Codex hook process chain changed")


def command_windows(python: Path, handler: Path, marker: Path) -> str:
    arguments = [str(python), str(handler), "handler", "--marker", str(marker)]
    return "& " + " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)


def command_windows_cmd(python: Path, handler: Path, marker: Path) -> str:
    arguments = [str(python), str(handler), "handler", "--marker", str(marker)]
    command = " ".join(f'"{argument.replace(chr(34), chr(34) * 2)}"' for argument in arguments)
    cmd_argument = f'"{command}"'
    powershell_quoted = "'" + cmd_argument.replace("'", "''") + "'"
    return f"& $env:ComSpec /d /s /c {powershell_quoted}"


def codex_version(codex: str) -> str:
    try:
        result = subprocess.run([codex, "--version"], capture_output=True, text=True, check=True)
    except OSError as error:
        raise RuntimeError(f"could not run Codex version command: {codex}") from error
    return result.stdout.strip()


def resolve_codex(command: str) -> str:
    """Resolve the standalone CLI without accidentally selecting a desktop host."""
    candidate = Path(command)
    if candidate.is_file():
        return str(candidate.resolve())
    resolved = shutil.which(command)
    if resolved is not None:
        return resolved
    raise RuntimeError(
        "could not find the Codex CLI executable; add codex to PATH or pass --codex PATH"
    )


def require_standalone_cli() -> None:
    """Avoid taking control of the desktop app's shared local runtime."""
    if os.environ.get("CODEX_APP_TOOLS_PIPE_PATH") or os.environ.get("CODEX_THREAD_ID"):
        raise RuntimeError("run this canary from a standalone PowerShell or cmd.exe console")


def profile_configuration(command: str, command_windows: str) -> str:
    """Render the temporary active config layer without serializing hook input."""
    return "\n".join(
        [
            "[[hooks.SessionStart]]",
            'matcher = "startup"',
            "",
            "[[hooks.SessionStart.hooks]]",
            'type = "command"',
            f"command = {json.dumps(command)}",
            f"commandWindows = {json.dumps(command_windows)}",
            "timeout = 3",
            "",
        ]
    )


def stop_owned_process_tree(
    process: subprocess.Popen, job: OwnedProcessJob | None
) -> tuple[str, str]:
    """Close the owning job, then collect the CLI process without touching others."""
    if job is not None:
        job.close()
    if process.poll() is None:
        process.kill()
    try:
        return process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.communicate()


def run_canary(args: argparse.Namespace) -> None:
    if os.name != "nt":
        raise RuntimeError("the Codex command-hook canary currently requires Windows")
    require_standalone_cli()
    codex = resolve_codex(args.codex)
    version = codex_version(codex)
    with tempfile.TemporaryDirectory(prefix="watchdog codex hook canary ") as temporary:
        root = Path(temporary)
        try:
            subprocess.run(["git", "init", str(root)], capture_output=True, text=True, check=True)
        except OSError as error:
            raise RuntimeError(
                "could not run git init for the temporary canary repository"
            ) from error
        marker = root / "marker.json"
        failure_marker = marker.with_suffix(".failure.json")
        handler = Path(__file__).resolve()
        command = [sys.executable, str(handler), "handler", "--marker", str(marker)]
        command_text = shlex.join(command)
        command_windows_text = (
            command_windows_cmd(Path(sys.executable), handler, marker)
            if args.launcher == "cmd"
            else command_windows(Path(sys.executable), handler, marker)
        )
        home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        profile_name = f"watchdog-canary-{uuid4().hex}"
        config = home / f"{profile_name}.config.toml"
        if config.exists():
            raise RuntimeError("unexpected existing temporary canary profile")
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            profile_configuration(command_text, command_windows_text),
            encoding="utf-8",
            newline="\n",
        )
        command_line = [
            codex,
            "exec",
            "--enable",
            "hooks",
            "--profile",
            profile_name,
            "--model",
            args.model,
            "--ephemeral",
            "--dangerously-bypass-hook-trust",
            "--sandbox",
            "read-only",
            "-C",
            str(root),
            "Reply with exactly CANARY_OK. Do not call tools.",
        ]
        process: subprocess.Popen | None = None
        job: OwnedProcessJob | None = None
        saved: dict | None = None
        failure: RuntimeError | None = None
        stderr = ""
        try:
            try:
                process = subprocess.Popen(
                    command_line,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
                )
            except OSError as error:
                raise RuntimeError(f"could not start Codex CLI: {codex}") from error
            job = OwnedProcessJob(process)
            deadline = time.monotonic() + args.hook_timeout
            while not marker.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            if not marker.is_file():
                if failure_marker.is_file():
                    failure_document = json.loads(failure_marker.read_text(encoding="utf-8"))
                    reason = failure_document.get("reason")
                    failure = RuntimeError(
                        f"canary handler failed before recording SessionStart: {reason}"
                    )
                else:
                    exit_code = process.poll()
                    if exit_code is None:
                        failure = RuntimeError(
                            "Codex did not run the SessionStart canary hook in time"
                        )
                    else:
                        failure = RuntimeError(
                            "Codex exited before running the SessionStart canary hook "
                            f"(code {exit_code})"
                        )
            else:
                saved = json.loads(marker.read_text(encoding="utf-8"))
        finally:
            if process is not None:
                _, stderr = stop_owned_process_tree(process, job)
            config.unlink(missing_ok=True)
    if failure is not None:
        if stderr.strip():
            detail = stderr.strip().replace("\r", "").replace("\n", " ")[-1000:]
            failure = RuntimeError(f"{failure}; Codex stderr: {detail}")
        raise failure
    if saved is None:
        raise RuntimeError("Codex canary did not produce a marker")
    report = {"codex_version": version, **saved}
    if args.record:
        print(json.dumps(report, sort_keys=True))
        return
    if version != args.expected_version:
        raise RuntimeError(
            f"Codex version changed: expected {args.expected_version}, got {version}"
        )
    validate_marker(saved, args.expected_process_chain.split(","))
    print(json.dumps(report, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    handler = subcommands.add_parser("handler", help="internal hook handler")
    handler.add_argument("--marker", type=Path, required=True)
    probe = subcommands.add_parser("probe", help="run an isolated Codex CLI canary")
    probe.add_argument("--codex", default="codex")
    probe.add_argument("--model", default="gpt-5.6-luna")
    probe.add_argument("--hook-timeout", type=float, default=30)
    probe.add_argument(
        "--launcher",
        choices=("powershell", "cmd"),
        default="powershell",
        help="commandWindows body to verify; use cmd only after recording the PowerShell baseline",
    )
    probe.add_argument(
        "--record", action="store_true", help="print a baseline instead of verifying it"
    )
    probe.add_argument("--expected-version")
    probe.add_argument("--expected-process-chain")
    args = parser.parse_args()
    if args.action == "handler":
        handle(args.marker, sys.stdin)
        print("{}")
        return 0
    if not args.record and (not args.expected_version or not args.expected_process_chain):
        parser.error("verification requires --expected-version and --expected-process-chain")
    try:
        run_canary(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"canary failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
