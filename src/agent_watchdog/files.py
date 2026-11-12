"""Atomic local writes shared by configuration, storage, and daemon controls."""

import os
import sys
import tempfile
import time
from pathlib import Path
from typing import BinaryIO

if sys.platform == "win32":
    from agent_watchdog._windows_files import open_shared_read, replace_shared
else:

    def open_shared_read(path: Path) -> BinaryIO:
        return path.open("rb")


def atomic_write(path: Path, content: bytes, *, allow_open_readers: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 0.1
        while True:
            try:
                if sys.platform == "win32" and allow_open_readers:
                    replace_shared(temporary, path)
                else:
                    temporary.replace(path)
                break
            except PermissionError as error:
                # Windows readers can briefly deny replacement even with cooperative writers.
                if getattr(error, "winerror", None) not in (5, 32) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
