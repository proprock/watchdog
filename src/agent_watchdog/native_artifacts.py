"""Select and install checksum-verified native adapter release artifacts."""

import hashlib
import json
import os
import platform
import tempfile
import zipfile
from pathlib import Path

PACKAGE = "agent-watchdog-hook"
# Intel macOS is deliberately not a declared target: GitHub retired standalone
# Intel-hosted runners (macos-13), and Apple Silicon is now several
# generations into replacing Intel Macs entirely.
_TARGETS = {
    ("Windows", "AMD64"): ("x86_64-pc-windows-msvc", ".exe"),
    ("Linux", "x86_64"): ("x86_64-unknown-linux-gnu", ""),
    ("Darwin", "arm64"): ("aarch64-apple-darwin", ""),
}


def target_for_host(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    """Return the release target and executable name for a supported host."""
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    normalized = (
        system,
        {"x86_64": "x86_64", "AMD64": "AMD64", "arm64": "arm64"}.get(machine, machine),
    )
    try:
        target, suffix = _TARGETS[normalized]
    except KeyError as error:
        supported = ", ".join(f"{name}/{arch}" for name, arch in sorted(_TARGETS))
        raise ValueError(
            f"Unsupported native adapter target: {system}/{machine}; supported hosts: {supported}"
        ) from error
    return target, PACKAGE + suffix


def archive_name(version: str, target: str) -> str:
    return f"{PACKAGE}-v{version}-{target}.zip"


def executable_for_target(target: str) -> str:
    """Return the provider-independent binary name for a declared Rust target."""
    for known_target, suffix in _TARGETS.values():
        if target == known_target:
            return PACKAGE + suffix
    supported = ", ".join(sorted({known_target for known_target, _ in _TARGETS.values()}))
    raise ValueError(
        f"Unsupported native adapter release target: {target}; supported targets: {supported}"
    )


def _manifest(archive: zipfile.ZipFile) -> dict[str, str | int]:
    try:
        raw = archive.read("manifest.json")
        manifest = json.loads(raw)
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Native adapter artifact has no valid manifest") from error
    required = {"schema_version", "package", "version", "target", "executable", "sha256"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError("Native adapter artifact manifest has an unsupported shape")
    text_fields = required - {"schema_version", "package"}
    if (
        manifest["schema_version"] != 1
        or manifest["package"] != PACKAGE
        or not all(isinstance(manifest[key], str) and manifest[key] for key in text_fields)
    ):
        raise ValueError("Native adapter artifact manifest is invalid")
    return manifest


def install_artifact(
    artifact: Path,
    destination: Path,
    *,
    system: str | None = None,
    machine: str | None = None,
    install: bool = True,
) -> Path:
    """Verify and optionally copy this host's release binary to a stable location."""
    target, executable = target_for_host(system, machine)
    if not artifact.is_absolute() or not artifact.is_file():
        raise ValueError("Choose an existing absolute native adapter artifact")
    try:
        with zipfile.ZipFile(artifact) as archive:
            manifest = _manifest(archive)
            if manifest["target"] != target or manifest["executable"] != executable:
                artifact_target = manifest["target"]
                diagnostic = "Native adapter artifact target "
                raise ValueError(
                    f"{diagnostic}{artifact_target} does not match this host ({target})"
                )
            version = str(manifest["version"])
            if artifact.name != archive_name(version, target):
                raise ValueError("Native adapter artifact filename does not match its manifest")
            members = {entry.filename for entry in archive.infolist() if not entry.is_dir()}
            if members != {"manifest.json", executable}:
                raise ValueError("Native adapter artifact has unexpected files")
            payload = archive.read(executable)
    except zipfile.BadZipFile as error:
        raise ValueError("Native adapter artifact is not a ZIP file") from error
    digest = hashlib.sha256(payload).hexdigest()
    if digest != manifest["sha256"]:
        raise ValueError("Native adapter artifact checksum does not match its manifest")

    target_dir = destination / version / target
    target_path = target_dir / executable
    if not install:
        return target_path
    if target_path.is_file():
        if hashlib.sha256(target_path.read_bytes()).hexdigest() == digest:
            return target_path
        raise ValueError("Installed native adapter conflicts with the selected artifact")
    target_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target_dir, delete=False) as output:
        output.write(payload)
        temporary = Path(output.name)
    try:
        if os.name != "nt":
            temporary.chmod(0o755)
        os.replace(temporary, target_path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    return target_path
