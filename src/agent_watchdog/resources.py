"""Project disk accounting and fixed-size, best-effort loss diagnostics."""

import os
import shutil
import struct
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from agent_watchdog.config import Limits

REASONS = ("quota", "payload", "invalid", "io", "busy")
_COUNTERS = struct.Struct("<5Q")


def usage(root: Path) -> int:
    total = 0
    if root.is_symlink() or root.is_junction():
        raise OSError("Linked data directories are not supported")
    for directory, folders, files in os.walk(root, followlinks=False):
        base = Path(directory)
        folders[:] = [
            name
            for name in folders
            if not (base / name).is_symlink() and not (base / name).is_junction()
        ]
        for name in files:
            path = base / name
            if not path.is_symlink():
                try:
                    total += path.stat().st_size
                except FileNotFoundError:
                    pass
    return total


def available(root: Path, limits: Limits) -> int:
    return (
        min(limits.project_bytes - usage(root), shutil.disk_usage(root).free) - limits.reserve_bytes
    )


def losses(root: Path) -> dict[str, int]:
    try:
        with (root / "losses.bin").open("rb") as stream:
            raw = stream.read(_COUNTERS.size + 1)
    except FileNotFoundError:
        return dict.fromkeys(REASONS, 0)
    if len(raw) != _COUNTERS.size:
        raise ValueError("Invalid loss counters")
    return dict(zip(REASONS, _COUNTERS.unpack(raw), strict=True))


def count_loss(root: Path, reason: str, count: int = 1) -> bool:
    from agent_watchdog.storage import StorageError

    try:
        root.mkdir(parents=True, exist_ok=True)
        with ExitStack() as stack:
            with _locked(root / "losses.lock"):
                values = losses(root)
                values[reason] = min(2**64 - 1, values[reason] + count)
                path = root / "losses.bin"
                # Reuse allocated bytes so ENOSPC does not need a temporary file.
                stream = stack.enter_context(
                    path.open("r+b" if path.exists() else "w+b", buffering=0)
                )
                stream.write(_COUNTERS.pack(*(values[key] for key in REASONS)))
            # The unbuffered update is visible; slow disk sync need not hold the lock.
            os.fsync(stream.fileno())
        return True
    except (OSError, StorageError, ValueError):
        return False


@contextmanager
def admission(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with _locked(root / "admission.lock"):
        yield


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    from agent_watchdog.storage import WriterBusy, writer_lock

    deadline = time.monotonic() + 0.1
    while True:
        lock = writer_lock(path)
        try:
            lock.__enter__()
            break
        except WriterBusy:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.005)
    try:
        yield
    finally:
        lock.__exit__(None, None, None)
