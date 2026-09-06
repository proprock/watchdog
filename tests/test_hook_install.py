import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from agent_watchdog.config import UserPaths
from agent_watchdog.hook_install import change, group

REALISTIC_SETTINGS: dict = {
    "theme": "dark",
    "autoUpdatesChannel": "stable",
    "enabledPlugins": ["a", "b"],
    "extraKnownMarketplaces": {"m": {"source": "x"}},
    "attribution": {"coAuthoredBy": False},
    "tui": {"scrollback": 1000},
    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo user"}]}]},
}


def test_native_install_preserves_ownership_and_uninstalls_without_binary(tmp_path):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    binary = tmp_path / "native adapter"
    binary.write_bytes(b"fixture")
    target = tmp_path / "hooks.json"
    change(target, paths, install=True, apply=True, adapter_executable=binary)
    document = json.loads(target.read_text())
    command = document["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert str(binary) in command and "--python" in command
    assert not change(target, paths, install=True, apply=True, adapter_executable=binary)["changed"]
    with pytest.raises(ValueError, match="Installation paths changed"):
        change(target, paths, install=True, apply=True)
    binary.unlink()
    change(target, paths, install=False, apply=True)
    assert not target.exists()


@pytest.fixture
def paths(tmp_path):
    return UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")


def test_dry_run_and_idempotent_round_trip_preserve_original(paths, tmp_path):
    target = tmp_path / "hooks.json"
    original = (
        b'{ "description":"mine", "hooks":{"Stop":[{"hooks":'
        b'[{"type":"command","command":"echo mine"}]}]} }\n'
    )
    target.write_bytes(original)
    before = set(tmp_path.iterdir())
    preview = change(target, paths, install=True, apply=False)
    assert preview["changed"]
    assert set(tmp_path.iterdir()) == before
    assert target.read_bytes() == original
    first = change(target, paths, install=True, apply=True)
    assert Path(first["backup"]).read_bytes() == original
    installed = target.read_bytes()
    assert not change(target, paths, install=True, apply=True)["changed"]
    assert target.read_bytes() == installed
    change(target, paths, install=False, apply=True)
    assert target.read_bytes() == original
    assert not change(target, paths, install=False, apply=True)["changed"]


def test_uninstall_preserves_later_user_changes(paths, tmp_path):
    target = tmp_path / "hooks.json"
    change(target, paths, install=True, apply=True)
    document = json.loads(target.read_text())
    user_group = {"hooks": [{"type": "command", "command": "echo keep"}]}
    document["hooks"]["Stop"].append(user_group)
    target.write_text(json.dumps(document))
    change(target, paths, install=False, apply=True)
    assert json.loads(target.read_text())["hooks"] == {"Stop": [user_group]}


def test_edited_owned_hook_is_not_overwritten(paths, tmp_path):
    target = tmp_path / "hooks.json"
    change(target, paths, install=True, apply=True)
    document = json.loads(target.read_text())
    document["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 99
    target.write_text(json.dumps(document))
    before = target.read_bytes()
    with pytest.raises(ValueError):
        change(target, paths, install=False, apply=True)
    assert target.read_bytes() == before


@pytest.mark.parametrize("content", [b"{", b"[]", b'{"hooks":[]}', b'{"hooks":{},"hooks":{}}'])
def test_bad_config_is_not_changed(paths, tmp_path, content):
    target = tmp_path / "hooks.json"
    target.write_bytes(content)
    with pytest.raises(ValueError):
        change(target, paths, install=True, apply=True)
    assert target.read_bytes() == content


def test_generated_command_executes_with_shell_metacharacters(tmp_path):
    home = tmp_path / "space ' quote $dollar & symbol"
    paths = UserPaths(home / "config.toml", home / "data", home / "runtime")
    handler = group(paths, "test-token")["hooks"][0]
    command = (
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", handler["commandWindows"]]
        if os.name == "nt"
        else ["/bin/sh", "-c", handler["command"]]
    )
    result = subprocess.run(command, input="{}", capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "{}"
    assert shlex.split(handler["command"])[0] == sys.executable
    assert not home.exists()


def test_empty_user_event_groups_survive_round_trip(paths, tmp_path):
    target = tmp_path / "hooks.json"
    original = b'{"hooks": {"Stop": []}}\n'
    target.write_bytes(original)
    change(target, paths, install=True, apply=True)
    change(target, paths, install=False, apply=True)
    assert target.read_bytes() == original


def test_interrupted_install_can_be_finished(paths, tmp_path, monkeypatch):
    from agent_watchdog import hook_install

    target = tmp_path / "hooks.json"
    write = hook_install.atomic_write

    def fail_target(path, content):
        if path == target:
            raise OSError("Simulated interrupted write")
        write(path, content)

    monkeypatch.setattr(hook_install, "atomic_write", fail_target)
    with pytest.raises(OSError):
        change(target, paths, install=True, apply=True)
    assert not target.exists()
    monkeypatch.setattr(hook_install, "atomic_write", write)
    change(target, paths, install=True, apply=True)
    change(target, paths, install=False, apply=True)
    assert not target.exists()


def test_missing_ownership_record_does_not_claim_uninstall(paths, tmp_path):
    target = tmp_path / "hooks.json"
    change(target, paths, install=True, apply=True)
    target.with_name("hooks.json.watchdog.json").unlink()
    before = target.read_bytes()
    with pytest.raises(ValueError):
        change(target, paths, install=False, apply=True)
    assert target.read_bytes() == before


@pytest.mark.parametrize("content", [b"null", b'{"schema_version":2}'])
def test_invalid_ownership_record_is_preserved(paths, tmp_path, content):
    target = tmp_path / "hooks.json"
    record = target.with_name("hooks.json.watchdog.json")
    record.write_bytes(content)
    with pytest.raises(ValueError):
        change(target, paths, install=True, apply=True)
    assert record.read_bytes() == content
    assert not target.exists()


@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_claude_install_uses_exec_form_and_round_trips(paths, tmp_path, name):
    target = tmp_path / name
    original = json.dumps(REALISTIC_SETTINGS, indent=2).encode() + b"\n"
    target.write_bytes(original)
    change(target, paths, install=True, apply=True, provider="claude")
    handler = json.loads(target.read_text())["hooks"]["Stop"][-1]["hooks"][0]
    assert handler["command"] == sys.executable
    assert handler["args"][:2] == ["-m", "agent_watchdog"]
    assert handler["args"][-4:-1] == ["hook", "claude", "--installation"]
    assert "commandWindows" not in handler
    assert "matcher" not in json.loads(target.read_text())["hooks"]["Stop"][-1]
    assert handler["timeout"] == 2
    assert not change(target, paths, install=True, apply=True, provider="claude")["changed"]
    change(target, paths, install=False, apply=True, provider="claude")
    assert target.read_bytes() == original


def test_claude_install_preserves_unrelated_settings_and_user_hooks(paths, tmp_path):
    target = tmp_path / "settings.json"
    original = json.dumps(REALISTIC_SETTINGS, indent=2).encode() + b"\n"
    target.write_bytes(original)
    change(target, paths, install=True, apply=True, provider="claude")
    installed = json.loads(target.read_text())
    assert installed["theme"] == "dark"
    assert installed["enabledPlugins"] == ["a", "b"]
    assert installed["hooks"]["Stop"][0] == REALISTIC_SETTINGS["hooks"]["Stop"][0]
    change(target, paths, install=False, apply=True, provider="claude")
    assert target.read_bytes() == original


def test_claude_native_install_records_provider_and_uninstalls_without_binary(paths, tmp_path):
    binary = tmp_path / "native adapter"
    binary.write_bytes(b"fixture")
    target = tmp_path / "settings.json"
    change(target, paths, install=True, apply=True, adapter_executable=binary, provider="claude")
    record = json.loads(target.with_name("settings.json.watchdog.json").read_text())
    assert record["provider"] == "claude"
    handler = json.loads(target.read_text())["hooks"]["Stop"][0]["hooks"][0]
    assert handler["command"] == str(binary)
    assert handler["args"][:2] == ["--python", sys.executable]
    binary.unlink()
    change(target, paths, install=False, apply=True, provider="claude")
    assert not target.exists()


@pytest.mark.parametrize(
    "name,provider",
    [("hooks.json", "claude"), ("settings.json", "codex"), ("settings.local.json", "codex")],
)
def test_cross_provider_file_mismatch_is_refused(paths, tmp_path, name, provider):
    target = tmp_path / name
    with pytest.raises(ValueError):
        change(target, paths, install=True, apply=True, provider=provider)
    assert not target.exists()


def test_legacy_codex_manifest_without_provider_still_uninstalls(paths, tmp_path):
    target = tmp_path / "hooks.json"
    change(target, paths, install=True, apply=True)
    record_path = target.with_name("hooks.json.watchdog.json")
    record = json.loads(record_path.read_text())
    record.pop("provider", None)
    record_path.write_text(json.dumps(record))
    change(target, paths, install=False, apply=True)
    assert not target.exists()


def test_claude_symlink_target_is_refused(paths, tmp_path):
    real = tmp_path / "real.json"
    real.write_bytes(b'{"hooks":{}}\n')
    link = tmp_path / "settings.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    with pytest.raises(ValueError):
        change(link, paths, install=True, apply=True, provider="claude")
