"""Terminate the actual interpreter, including behind Windows venv launchers."""

import os
import sys
import threading
import time
from pathlib import Path

from agent_watchdog.config import UserPaths
from agent_watchdog.daemon import run


def crash_on_request(root: Path) -> None:
    deadline = time.monotonic() + 20
    while not (root / "crash").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    os._exit(91)


root = Path(sys.argv[1])
threading.Thread(target=crash_on_request, args=(root,), daemon=True).start()
run(UserPaths(root / "config.toml", root / "data", root / "runtime"))
