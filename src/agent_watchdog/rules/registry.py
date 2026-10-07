"""Which declarative rules exist and which of them may run (WD-142).

A rule's status is derived here, never read from the rule file: a file cannot
approve itself. Built-ins ship with the package and are trusted as the code is.
A user rule is ``approved`` only while the SHA-256 of its current bytes equals
the digest the user approved; any edit makes it ``stale`` and it is not applied.
"""

import hashlib
import os
import threading
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Literal

from agent_watchdog.config import Config, Rules, UserPaths
from agent_watchdog.rules.api import renderable
from agent_watchdog.rules.declarative import DeclarativeRule, RuleError, parse_rule

Status = Literal["approved", "proposed", "stale", "disabled", "invalid"]
Source = Literal["builtin", "user"]

# A rule file is a few hundred bytes; the bound keeps a stray large file from
# being read on every change and caps what `approve` could ever show.
MAX_RULE_BYTES = 64 * 1024
MAX_RULE_FILES = 128


@dataclass(frozen=True, slots=True)
class RuleEntry:
    name: str
    source: Source
    status: Status
    rule: DeclarativeRule | None = None
    digest: str | None = None
    text: str | None = None
    error: str | None = None
    path: Path | None = None

    @property
    def active(self) -> bool:
        return self.status == "approved" and self.rule is not None


@dataclass(frozen=True, slots=True)
class _Loaded:
    """A parsed file before the configuration says what may run."""

    name: str
    source: Source
    rule: DeclarativeRule | None
    digest: str | None
    text: str | None
    error: str | None
    path: Path | None


def _parse(data: bytes, name: str, source: Source, path: Path | None) -> _Loaded:
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
        rule = parse_rule(text)
    except UnicodeDecodeError:
        return _Loaded(name, source, None, digest, None, "The rule file is not valid UTF-8", path)
    except RuleError as error:
        return _Loaded(name, source, None, digest, text, str(error), path)
    if rule.name != name:
        message = f"The rule name {rule.name} does not match its file name {name}"
        return _Loaded(name, source, None, digest, text, message, path)
    return _Loaded(name, source, rule, digest, text, None, path)


@cache
def _builtins() -> tuple[_Loaded, ...]:
    folder = resources.files("agent_watchdog.rules") / "builtin"
    found = [
        _parse(item.read_bytes(), item.name.removesuffix(".toml"), "builtin", None)
        for item in sorted(folder.iterdir(), key=lambda item: item.name)
        if item.name.endswith(".toml")
    ]
    return tuple(found)


BUILTIN_NAMES: tuple[str, ...] = tuple(item.name for item in _builtins())


def _scan(folder: Path) -> tuple[tuple[tuple[str, int, int], ...], tuple[Path, ...]]:
    """The ``*.toml`` files directly in ``folder`` with a change signature; none if absent."""
    try:
        with os.scandir(folder) as listing:
            files = sorted(
                (item for item in listing if item.name.endswith(".toml") and item.is_file()),
                key=lambda item: item.name,
            )[:MAX_RULE_FILES]
            signature = tuple(
                (item.name, (info := item.stat()).st_mtime_ns, info.st_size) for item in files
            )
            return signature, tuple(Path(item.path) for item in files)
    except OSError:
        return (), ()


def _load_user(paths: tuple[Path, ...]) -> tuple[_Loaded, ...]:
    loaded = []
    for path in paths:
        name = path.stem
        try:
            if path.stat().st_size > MAX_RULE_BYTES:
                loaded.append(
                    _Loaded(name, "user", None, None, None, "The rule file is too large", path)
                )
                continue
            data = path.read_bytes()
        except OSError:
            loaded.append(
                _Loaded(name, "user", None, None, None, "The rule file is unreadable", path)
            )
            continue
        item = _parse(data, name, "user", path)
        if name in BUILTIN_NAMES:
            item = _Loaded(
                name,
                "user",
                None,
                item.digest,
                item.text,
                "The name belongs to a built-in rule",
                path,
            )
        loaded.append(item)
    return tuple(loaded)


def _status(item: _Loaded, rules: Rules) -> Status:
    if item.rule is None:
        return "invalid"
    if item.name in rules.disabled:
        return "disabled"
    if item.source == "builtin":
        if not item.rule.default_enabled and item.name not in rules.enabled:
            return "disabled"
        return "approved"
    approved = rules.approved.get(item.name)
    if approved is None:
        return "proposed"
    return "approved" if approved == item.digest else "stale"


class RuleRegistry:
    """Rule discovery with a cache keyed by the rules directory and the configuration.

    Safe to call from the decision-channel threads and the poll thread: a call
    reloads only when a file's modification time or size, or the ``[rules]``
    configuration, changed since the previous one.
    """

    def __init__(self, paths: UserPaths) -> None:
        self._folder = paths.config.parent / "rules"
        self._lock = threading.Lock()
        self._signature: tuple[tuple[str, int, int], ...] | None = None
        self._loaded: tuple[_Loaded, ...] = ()
        self._rules: Rules | None = None
        self._entries: tuple[RuleEntry, ...] = ()

    def entries(self, config: Config) -> tuple[RuleEntry, ...]:
        """Built-ins first, then user files by name, each with its derived status."""
        signature, files = _scan(self._folder)
        with self._lock:
            if signature != self._signature:
                self._loaded = _builtins() + _load_user(files)
                self._signature, self._rules = signature, None
            if self._rules != config.rules or self._rules is None:
                self._entries = tuple(
                    RuleEntry(
                        item.name,
                        item.source,
                        _status(item, config.rules),
                        item.rule,
                        item.digest,
                        item.text,
                        item.error,
                        item.path,
                    )
                    for item in self._loaded
                )
                self._rules = config.rules
            return self._entries

    def active(self, config: Config) -> tuple[RuleEntry, ...]:
        """Runnable rules in evaluation order: built-ins, then user rules, each by priority."""
        runnable = [entry for entry in self.entries(config) if entry.active]
        return tuple(
            sorted(
                runnable,
                key=lambda entry: (
                    entry.source != "builtin",
                    entry.rule.priority if entry.rule else 0,
                    entry.name,
                ),
            )
        )

    def subscriptions(self, config: Config) -> list[dict[str, str | None]]:
        """The hook events the adapter should ask the daemon about, one entry per cell."""
        cells: set[tuple[str, str, str | None]] = set()
        for entry in self.active(config):
            rule = entry.rule
            if rule is None:
                continue
            for provider in rule.provider:
                if rule.action.kind == "log" or renderable(provider, rule.event, rule.action.kind):
                    cells.add((provider, rule.event, rule.match.tool_name))
        return [
            {"provider": provider, "hook_event_name": event, "tool_name": tool}
            for provider, event, tool in sorted(
                cells, key=lambda cell: (cell[0], cell[1], cell[2] or "")
            )
        ]
