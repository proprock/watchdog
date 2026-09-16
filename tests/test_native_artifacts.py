import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from agent_watchdog.cli import main
from agent_watchdog.config import UserPaths
from agent_watchdog.hook_install import change
from agent_watchdog.native_artifacts import archive_name, install_artifact, target_for_host


def make_artifact(directory: Path, *, target: str, executable: str, version: str = "0.1.0") -> Path:
    payload = b"native-adapter-fixture"
    manifest = {
        "schema_version": 1,
        "package": "agent-watchdog-hook",
        "version": version,
        "target": target,
        "executable": executable,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    archive = directory / archive_name(version, target)
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(executable, payload)
        output.writestr("manifest.json", json.dumps(manifest))
    return archive


def test_installed_artifact_is_selected_for_the_host_without_cargo(tmp_path, monkeypatch):
    target, executable = target_for_host("Windows", "AMD64")
    artifact = make_artifact(tmp_path, target=target, executable=executable)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    monkeypatch.setenv("PATH", "")

    installed = install_artifact(
        artifact, paths.data / "native-adapters", system="Windows", machine="AMD64"
    )

    assert installed == paths.data / "native-adapters" / "0.1.0" / target / executable
    assert installed.read_bytes() == b"native-adapter-fixture"
    assert os.environ["PATH"] == ""


def test_hook_installer_accepts_matching_release_artifact(tmp_path, monkeypatch):
    target, executable = target_for_host()
    artifact = make_artifact(tmp_path, target=target, executable=executable)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    monkeypatch.setenv("PATH", "")

    change(tmp_path / "hooks.json", paths, install=True, apply=True, adapter_artifact=artifact)

    command = json.loads((tmp_path / "hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0][
        "command"
    ]
    assert str(paths.data / "native-adapters" / "0.1.0" / target / executable) in command


def test_release_artifact_dry_run_does_not_write_a_binary(tmp_path):
    target, executable = target_for_host()
    artifact = make_artifact(tmp_path, target=target, executable=executable)
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")

    preview = change(
        tmp_path / "hooks.json",
        paths,
        install=True,
        apply=False,
        adapter_artifact=artifact,
    )

    assert preview["changed"]
    assert not paths.data.exists()


def test_cli_selects_matching_release_artifact(tmp_path, monkeypatch, capsys):
    target, executable = target_for_host()
    artifact = make_artifact(tmp_path, target=target, executable=executable)
    home = tmp_path / "home"
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-watchdog",
            "--home",
            str(home),
            "hooks",
            "install",
            "codex",
            "--file",
            str(tmp_path / "hooks.json"),
            "--adapter-artifact",
            str(artifact),
            "--apply",
        ],
    )

    assert main() == 0

    document = json.loads((tmp_path / "hooks.json").read_text())
    command = document["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert str(home / "data" / "native-adapters" / "0.1.0" / target / executable) in command
    assert json.loads(capsys.readouterr().out)["changed"]


@pytest.mark.parametrize(
    ("system", "machine"),
    # Intel macOS is deliberately undeclared: GitHub retired standalone
    # Intel-hosted macOS runners, and Apple Silicon has replaced Intel Macs.
    [("Linux", "armv7l"), ("Plan9", "x86_64"), ("Darwin", "x86_64")],
)
def test_unsupported_host_has_an_explicit_diagnostic(system, machine):
    with pytest.raises(ValueError, match="Unsupported native adapter target"):
        target_for_host(system, machine)


def test_artifact_for_another_target_is_refused(tmp_path):
    target, executable = target_for_host("Windows", "AMD64")
    artifact = make_artifact(tmp_path, target=target, executable=executable)

    with pytest.raises(ValueError, match="does not match this host"):
        install_artifact(artifact, tmp_path / "installed", system="Linux", machine="x86_64")


def test_packaging_script_makes_an_installable_release_archive(tmp_path):
    target, executable = target_for_host()
    binary = tmp_path / executable
    binary.write_bytes(b"native-adapter-fixture")
    output = tmp_path / "release"

    result = subprocess.run(
        [
            sys.executable,
            "scripts/package_native_artifact.py",
            "--binary",
            str(binary),
            "--target",
            target,
            "--version",
            "0.1.0",
            "--output-dir",
            str(output),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    archive = output / archive_name("0.1.0", target)
    assert Path(result.stdout.strip()) == archive
    assert install_artifact(archive, tmp_path / "installed") == (
        tmp_path / "installed" / "0.1.0" / target / executable
    )
