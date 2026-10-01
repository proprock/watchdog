"""Process helpers that keep optional work from competing with hook handling (WD-138)."""

import os
import subprocess
import sys

from agent_watchdog import _proc

PROBE = """
import ctypes, os, sys
from agent_watchdog import _proc

def priority():
    if os.name == "nt":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.GetPriorityClass.argtypes = [ctypes.c_void_p]
        return kernel32.GetPriorityClass(kernel32.GetCurrentProcess())
    return os.nice(0)

before = priority()
_proc.lower_own_priority()
print(before, priority())
"""


def test_lower_own_priority_lowers_only_the_calling_process():
    # A child process keeps the test runner's own priority untouched.
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        check=True,
        creationflags=_proc.hidden_creationflags(),
    )

    before, after = map(int, result.stdout.split())
    if os.name == "nt":
        assert after == _proc.BELOW_NORMAL_PRIORITY_CLASS
    else:
        assert after == min(before + 5, 19)
