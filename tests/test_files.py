from pathlib import Path

from agent_watchdog.storage import atomic_write


def test_atomic_write_retries_a_transient_windows_reader_lock(tmp_path, monkeypatch):
    target = tmp_path / "status.json"
    target.write_bytes(b"old")
    replace = Path.replace
    attempts = 0

    class ReaderLock(PermissionError):
        winerror = 5

    def temporarily_locked(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ReaderLock("Destination is open in another process")
        return replace(source, destination)

    monkeypatch.setattr(Path, "replace", temporarily_locked)
    atomic_write(target, b"new")
    assert target.read_bytes() == b"new"
    assert attempts == 2
    assert list(tmp_path.iterdir()) == [target]
