"""Session-wide setup for the offline test suite.

By default, on Windows, every child process spawned during the run starts without a
console window. Many tests spawn ``git``, ``python``, ``powershell`` or the native
adapter, and when pytest itself has no console (launched from a GUI) each child would
otherwise flash its own window. Set ``WATCHDOG_TEST_CONSOLE_WINDOWS`` to a truthy value
(``1``/``true``/``yes``/``on``) to keep the windows, e.g. for interactive debugging.

Suppression is a ``CREATE_NO_WINDOW`` flag added to ``subprocess.Popen``; it leaves
redirected stdio, pipes and timeouts untouched, and defers to callers that already ask
for ``DETACHED_PROCESS`` or ``CREATE_NEW_CONSOLE``.
"""

import os
import subprocess

from agent_watchdog._proc import hidden_creationflags

_TRUTHY = {"1", "true", "yes", "on"}
_original_popen_init = None


def _console_windows_allowed() -> bool:
    return os.environ.get("WATCHDOG_TEST_CONSOLE_WINDOWS", "").strip().lower() in _TRUTHY


def pytest_configure(config):
    global _original_popen_init
    if os.name != "nt" or _console_windows_allowed() or _original_popen_init is not None:
        return
    _original_popen_init = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        kwargs["creationflags"] = hidden_creationflags(kwargs.get("creationflags", 0))
        _original_popen_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = patched


def pytest_unconfigure(config):
    global _original_popen_init
    if _original_popen_init is not None:
        subprocess.Popen.__init__ = _original_popen_init
        _original_popen_init = None
