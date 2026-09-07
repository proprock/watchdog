"""Spawn child processes without flashing a console window on Windows.

Console-subsystem children (``git``, ``python``, ``powershell``, the native adapter)
allocate a fresh console when their parent has none, e.g. pytest launched from a GUI.
``CREATE_NO_WINDOW`` suppresses that console without affecting redirected stdio, so
pipes, ``capture_output``, ``communicate`` and line-buffered streaming are unchanged.
It must not be combined with ``DETACHED_PROCESS`` or ``CREATE_NEW_CONSOLE``; callers
that already select one of those keep their flags untouched.
"""

import os
import subprocess

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0
_CONSOLE_MODES = 0x00000010 | 0x00000008  # CREATE_NEW_CONSOLE | DETACHED_PROCESS


def hidden_creationflags(existing: int = 0) -> int:
    """Add ``CREATE_NO_WINDOW`` on Windows unless a console mode is already selected."""
    if os.name != "nt" or existing & _CONSOLE_MODES:
        return existing
    return existing | CREATE_NO_WINDOW


def run(*args, **kwargs):
    """``subprocess.run`` that suppresses the child console window on Windows."""
    if os.name == "nt":
        kwargs["creationflags"] = hidden_creationflags(kwargs.get("creationflags", 0))
    return subprocess.run(*args, **kwargs)


def popen(*args, **kwargs):
    """``subprocess.Popen`` that suppresses the child console window on Windows."""
    if os.name == "nt":
        kwargs["creationflags"] = hidden_creationflags(kwargs.get("creationflags", 0))
    return subprocess.Popen(*args, **kwargs)
