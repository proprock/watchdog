import json
import os
import shlex
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from wdi.llm.backend import ClaudeCliBackend, LLMError

from agent_watchdog import readapi
from agent_watchdog.insights import llm


@pytest.fixture
def synthetic_claude(tmp_path, monkeypatch):
    monkeypatch.setattr(readapi, "default_data_dir", lambda: tmp_path / "data")
    script = tmp_path / "claude.py"
    script.write_text(
        """import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(__file__).with_name('calls').open('a', encoding='utf-8') as log:
    log.write(args[0] + '\\n')
if args == ['--version']:
    print('synthetic Claude 1.0')
elif args == ['--help']:
    print('--safe-mode')
else:
    print(json.dumps({'result': json.dumps({
        'args': args, 'cwd': os.getcwd(), 'prompt': sys.stdin.read(),
    })}))
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        executable = tmp_path / "claude.exe"
        executable.touch()

        def launch(argv, **kwargs):
            assert Path(argv[0]) == executable
            return subprocess.run([sys.executable, "-X", "utf8", str(script), *argv[1:]], **kwargs)

        monkeypatch.setattr(llm._proc, "run", launch)
    else:
        executable = tmp_path / "claude"
        executable.write_text(
            f"#!/bin/sh\nexec {shlex.quote(sys.executable)} -X utf8 "
            f'{shlex.quote(str(script))} "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    return executable, tmp_path / "calls"


@pytest.mark.parametrize("reason", ["nonzero_exit", "is_error"])
def test_claude_backend_preserves_actionable_provider_error(tmp_path, monkeypatch, reason):
    monkeypatch.setattr(readapi, "default_data_dir", lambda: tmp_path / "unused")
    monkeypatch.setattr(
        llm,
        "claude",
        lambda *a, **k: llm.Result(
            None,
            reason,
            {
                "api_error_status": 429,
                "terminal_reason": "api_error",
                "result": "Weekly allowance exhausted; resets November 14 at 11:00 [UTC].",
                "argv": ["private executable"],
            },
        ),
    )

    with pytest.raises(LLMError) as caught:
        ClaudeCliBackend().complete("prompt", "haiku")

    message = str(caught.value)
    assert reason in message
    assert "429" in message
    assert "api_error" in message
    assert "resets November 14 at 11:00 [UTC]" in message
    assert "private executable" not in message


def test_claude_backend_uses_isolated_text_mode(synthetic_claude):
    executable, _ = synthetic_claude
    answer = json.loads(ClaudeCliBackend(executable=str(executable)).complete("hello", "haiku"))
    argv = answer["args"]
    assert {"--safe-mode", "--strict-mcp-config", "--no-session-persistence"} <= set(argv)
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--tools") + 1] == ""
    assert "--json-schema" not in argv
    assert argv[argv.index("--model") + 1] == "haiku"
    assert Path(answer["cwd"]) != Path.cwd()
    assert not Path(answer["cwd"]).exists()


def test_claude_backend_keeps_large_prompts_on_stdin(synthetic_claude):
    executable, _ = synthetic_claude
    prompt = "\N{LATIN SMALL LETTER A WITH OGONEK}" * (100 * 1024)
    answer = json.loads(ClaudeCliBackend(executable=str(executable)).complete(prompt, "haiku"))
    assert answer["prompt"] == prompt


def test_claude_backend_refuses_scratch_inside_a_manifest_root(synthetic_claude, tmp_path):
    executable, calls = synthetic_claude
    data = tmp_path / "data"
    store = data / "projects" / "registered"
    store.mkdir(parents=True)
    (store / "store.json").write_text(
        json.dumps(
            {
                "contract": 1,
                "machine_id": "fixture",
                "hostname": "fixture",
                "os": "fixture",
                "project_id": "registered",
                "name": "registered",
                "root": tempfile.gettempdir(),
                "git_common_dir": None,
                "events_schema": 1,
                "capture_schema": 1,
                "written_at": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    backend = ClaudeCliBackend(executable=str(executable))
    with pytest.raises(LLMError, match="isolation_unavailable"):
        backend.complete("private prompt", "haiku")
    assert "-p" not in calls.read_text(encoding="utf-8").splitlines()


def test_claude_backend_checks_configured_data_directories(synthetic_claude, tmp_path):
    executable, calls = synthetic_claude
    data = tmp_path / "configured"
    store = data / "projects" / "registered"
    store.mkdir(parents=True)
    # Unknown contract versions still expose roots when the manifest has them.
    (store / "store.json").write_text(
        json.dumps({"contract": readapi.CONTRACT_VERSION + 1, "root": tempfile.gettempdir()}),
        encoding="utf-8",
    )
    backend = ClaudeCliBackend(executable=str(executable), data_dirs=[data])
    with pytest.raises(LLMError, match="isolation_unavailable"):
        backend.complete("private prompt", "haiku")
    assert "-p" not in calls.read_text(encoding="utf-8").splitlines()


def test_claude_refuses_an_unresolvable_manifest_root(synthetic_claude, tmp_path, monkeypatch):
    executable, calls = synthetic_claude
    root = tmp_path / "unresolvable"
    store = tmp_path / "data" / "projects" / "registered"
    store.mkdir(parents=True)
    (store / "store.json").write_text(
        json.dumps({"contract": readapi.CONTRACT_VERSION + 1, "root": str(root)}),
        encoding="utf-8",
    )
    resolve = Path.resolve

    def resolve_root(path, *args, **kwargs):
        if path == root:
            raise OSError("synthetic root resolution failure")
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_root)
    with pytest.raises(LLMError, match="isolation_unavailable"):
        ClaudeCliBackend(executable=str(executable)).complete("private prompt", "haiku")
    assert "-p" not in calls.read_text(encoding="utf-8").splitlines()


def test_concurrent_claude_calls_share_preflight(synthetic_claude):
    executable, calls = synthetic_claude
    backend = ClaudeCliBackend(executable=str(executable))
    with ThreadPoolExecutor(max_workers=4) as pool:
        answers = list(pool.map(lambda prompt: backend.complete(prompt, "haiku"), "abcd"))
    assert [json.loads(answer)["prompt"] for answer in answers] == list("abcd")
    assert sorted(calls.read_text(encoding="utf-8").splitlines()) == [
        "--help",
        "--version",
        "-p",
        "-p",
        "-p",
        "-p",
    ]


def test_claude_preflight_refreshes_when_executable_changes(synthetic_claude):
    executable, calls = synthetic_claude
    backend = ClaudeCliBackend(executable=str(executable))
    backend.complete("before", "haiku")
    stat = executable.stat()
    os.utime(executable, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
    backend.complete("after", "haiku")
    assert calls.read_text(encoding="utf-8").splitlines() == ["--version", "--help", "-p"] * 2


@pytest.mark.skipif(os.name != "nt", reason="Windows batch launchers use the command shell")
def test_claude_refuses_windows_batch_launcher(tmp_path, monkeypatch):
    monkeypatch.setattr(readapi, "default_data_dir", lambda: tmp_path / "data")
    launcher = tmp_path / "claude.cmd"
    marker = tmp_path / "launched"
    launcher.write_text(f'@echo called > "{marker}"\n@echo --safe-mode\n', encoding="utf-8")
    with pytest.raises(LLMError, match="isolation_unavailable"):
        ClaudeCliBackend(executable=str(launcher)).complete("hello", "haiku")
    assert not marker.exists()
