"""Session-wide setup for the offline test suite.

By default, on Windows, every child process spawned during the run starts without a
console window. Many tests spawn ``git``, ``python``, ``powershell`` or the native
adapter, and when pytest itself has no console (launched from a GUI) each child would
otherwise flash its own window. Set ``WATCHDOG_TEST_CONSOLE_WINDOWS`` to a truthy value
(``1``/``true``/``yes``/``on``) to keep the windows, e.g. for interactive debugging.

Suppression is a ``CREATE_NO_WINDOW`` flag added to ``subprocess.Popen``; it leaves
redirected stdio, pipes and timeouts untouched, and defers to callers that already ask
for ``DETACHED_PROCESS`` or ``CREATE_NEW_CONSOLE``.

Real transcripts of the user's own sessions live under the git-ignored ``tests/local/``
as ``tests/local/<provider>/**/*.jsonl`` (``claude`` or ``codex``). A test that takes the
``corpus_transcript`` argument runs once per file and is reported as skipped when the
directory holds none, so the default offline suite is unchanged without a corpus.
"""

import os
import subprocess
from pathlib import Path

import pytest

from agent_watchdog import _proc
from agent_watchdog._proc import hidden_creationflags

_TRUTHY = {"1", "true", "yes", "on"}
LOCAL_CORPUS = Path(__file__).parent / "local"
CORPUS_PROVIDERS = ("claude", "codex")
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


@pytest.fixture(autouse=True)
def _keep_runner_priority():
    """In-process CLI calls must not lower the priority of the pytest process itself.

    Restores by hand: requesting ``monkeypatch`` here would reorder teardown for tests
    that patch module state while other fixtures still write during their own teardown.
    """
    original = _proc.lower_own_priority
    setattr(_proc, "lower_own_priority", lambda: None)  # noqa: B010
    yield
    setattr(_proc, "lower_own_priority", original)  # noqa: B010


def local_corpus(root: Path = LOCAL_CORPUS) -> list[tuple[str, Path]]:
    """Return sorted ``(provider, path)`` pairs for every transcript under ``root``."""
    return [
        (provider, path)
        for provider in CORPUS_PROVIDERS
        for path in sorted((root / provider).rglob("*.jsonl"))
    ]


def pytest_generate_tests(metafunc):
    if "corpus_transcript" in metafunc.fixturenames:
        corpus = local_corpus()
        metafunc.parametrize(
            "corpus_transcript",
            corpus,
            ids=[
                f"{provider}/{path.relative_to(LOCAL_CORPUS / provider).as_posix()}"
                for provider, path in corpus
            ],
        )
