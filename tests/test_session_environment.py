import subprocess
import time
from pathlib import Path


def test_instruction_imports_obey_depth_and_checkout_boundary(tmp_path):
    from agent_watchdog.session_environment import _capture_files

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    (checkout / "AGENTS.md").write_text("@one.md\n@../outside.md\n", encoding="utf-8")
    for name, child in (("one", "two"), ("two", "three"), ("three", "four")):
        (checkout / f"{name}.md").write_text(f"@{child}.md\n", encoding="utf-8")
    (checkout / "four.md").write_text("too deep", encoding="utf-8")
    files = _capture_files("codex", checkout, tmp_path / "home", time.monotonic() + 5, {})
    captured = {Path(file["path"]).name for file in files if file["path"] and file["digest"]}
    assert captured == {"AGENTS.md", "one.md", "two.md", "three.md"}


def test_environment_files_preserve_deadline_and_size_errors(tmp_path):
    from agent_watchdog.session_environment import _capture_files

    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"x" * (1024**2 + 1))
    for deadline, expected in ((0, "deadline"), (time.monotonic() + 5, "too_large")):
        blobs = {}
        files = _capture_files("codex", tmp_path, tmp_path / "home", deadline, blobs)
        entry = next(file for file in files if file["path"] == str(target))
        assert entry["error"] == expected and entry["digest"] is None
        assert not blobs


def test_initial_diff_preserves_tracked_provider_settings(tmp_path):
    from agent_watchdog.session_environment import _initial_diff

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(tmp_path), *args], check=True, capture_output=True
        ).stdout

    git("init")
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"token":"original-test-secret"}', encoding="utf-8")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("original\n", encoding="utf-8")
    git("add", ".")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    settings.write_text('{"token":"new-test-secret"}', encoding="utf-8")
    tracked.write_text("changed\n", encoding="utf-8")
    blobs = {}
    head = git("rev-parse", "HEAD").decode().strip()
    digest, error = _initial_diff(tmp_path, head, time.monotonic() + 5, blobs)
    assert error is None and digest is not None
    assert b"+changed" in blobs[digest]
    assert b'\n-{"token":"original-test-secret"}' in blobs[digest]
    assert b'\n+{"token":"new-test-secret"}' in blobs[digest]
