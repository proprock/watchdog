"""Incremental discovery and backfill of local provider transcript files."""

import json
import os
import sqlite3
import stat
import time
from collections import deque
from collections.abc import Callable, Generator, Iterator
from compression import zstd
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol

from agent_watchdog.capture import TICK_BUDGET, Source, _open_source, capture_file, stream_identity
from agent_watchdog.config import Config, ConfigError, Scan, UserPaths
from agent_watchdog.files import atomic_write
from agent_watchdog.ignored import IgnoredRecords, IgnoreReason, ScanIgnoreReason
from agent_watchdog.registry import Registry, RegistryError, Resolution
from agent_watchdog.storage import StorageError, WriterBusy
from agent_watchdog.transcripts import MAX_LINE_BYTES, UnsupportedTranscript

WALK_SECONDS = 0.05
HEAD_BYTES = 64 * 1024
QUEUE_LIMIT = 128
_PROVIDERS = ("claude", "codex")
_SCHEMA = """CREATE TABLE IF NOT EXISTS files(
    provider TEXT, path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER,
    project_id TEXT, decision TEXT, checked_at TEXT
)"""
_DECISIONS = """INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(path) DO UPDATE SET
        provider=excluded.provider, size=excluded.size, mtime_ns=excluded.mtime_ns,
        project_id=excluded.project_id, decision=excluded.decision, checked_at=excluded.checked_at
"""


class _Register(Protocol):
    def __call__(
        self, paths: UserPaths, path: Path, *, timeout: float
    ) -> tuple[Config, Resolution | IgnoreReason]: ...


@dataclass(frozen=True)
class _File:
    provider: str
    root: Path
    path: Path

    def sources(self) -> tuple[tuple[str, str] | None, Path | None]:
        if self.provider == "codex":
            return stream_identity("codex", self.path), None
        parts = self.path.relative_to(self.root).parts
        if len(parts) == 2:
            return stream_identity("claude", self.path), None
        if len(parts) >= 3:
            if len(parts) == 4 and parts[2] == "subagents" and self.path.suffix == ".jsonl":
                return stream_identity("claude", self.path), None
            return (parts[1], "main"), self.root / parts[0] / parts[1]
        return None, None


@dataclass(frozen=True)
class _Unreadable:
    provider: str


def _walk(provider: str, root: Path, directory: Path) -> Iterator[_File | _Unreadable | None]:
    # Yield for directories and skipped entries too, so empty trees are budgeted.
    yield None
    if directory.is_symlink() or directory.is_junction():
        return
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink() or entry.is_junction():
                    yield None
                elif entry.is_dir(follow_symlinks=False):
                    yield from _walk(provider, root, Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    yield _File(provider, root, Path(entry.path))
                else:
                    yield None
    except FileNotFoundError:
        return
    except OSError:
        yield _Unreadable(provider)


def _provider_files(settings: Scan) -> Generator[_File | _Unreadable | None]:
    claude = settings.claude_roots or (Path.home() / ".claude" / "projects",)
    codex_home = Path.home() / ".codex"
    codex = settings.codex_roots or (codex_home / "sessions", codex_home / "archived_sessions")
    for provider, roots in zip(_PROVIDERS, (claude, codex), strict=True):
        for root in roots:
            yield from _walk(provider, root, root)


def _head_lines(stream: BinaryIO, remaining: int) -> Iterator[bytes]:
    while remaining > 0 and (line := stream.readline(remaining)):
        remaining -= len(line)
        yield line


def _read_cwd(provider: str, path: Path) -> str | None:
    stream, _ = _open_source(path)
    with stream:
        limit = HEAD_BYTES if provider == "claude" else MAX_LINE_BYTES
        for line in _head_lines(stream, limit):
            try:
                item = json.loads(line)
            except ValueError, RecursionError:
                continue
            if not isinstance(item, dict):
                continue
            if provider == "codex":
                if item.get("type") != "session_meta":
                    continue
                payload = item.get("payload")
                cwd = payload.get("cwd") if isinstance(payload, dict) else None
                return cwd if isinstance(cwd, str) else None
            if isinstance(cwd := item.get("cwd"), str):
                return cwd
    return None


def _source_cwd(file: _File, stream_id: str, side_directory: Path | None) -> str | None:
    if side_directory is None:
        cwd = _read_cwd(file.provider, file.path)
        if cwd is not None or stream_id == "main":
            return cwd
        side_directory = file.path.parent.parent
    try:
        return _read_cwd("claude", side_directory.with_name(side_directory.name + ".jsonl"))
    except FileNotFoundError, UnsupportedTranscript:
        return None


def _existing_directory(cwd: str) -> Path:
    path = Path(cwd)
    if not path.is_absolute() or "\0" in cwd:
        raise RegistryError("Transcript cwd must name an absolute directory")
    for candidate in (path, *path.parents):
        try:
            info = candidate.stat()
        except FileNotFoundError, NotADirectoryError:
            continue
        if stat.S_ISDIR(info.st_mode):
            return candidate
        if candidate == path:
            break
    raise RegistryError("Transcript cwd has no existing directory")


def _timestamp(mtime_ns: int | None) -> str | None:
    if mtime_ns is None:
        return None
    return datetime.fromtimestamp(mtime_ns / 1_000_000_000, UTC).isoformat()


def _coverage(db: sqlite3.Connection, stamp: str, unreadable: dict[str, int]) -> dict:
    providers: dict[str, dict] = {
        name: {
            "files_on_disk": 0,
            "fully_captured": 0,
            "partly_captured": 0,
            "ignored": {},
            "unrecognized_names": 0,
            "unreadable_directories": unreadable[name],
            "last_finished_at": stamp,
            "oldest_mtime": None,
        }
        for name in _PROVIDERS
    }
    counts = {
        "complete": "fully_captured",
        "partial": "partly_captured",
        "unrecognized": "unrecognized_names",
    }
    for provider, decision, count, oldest in db.execute(
        "SELECT provider, decision, count(*), min(mtime_ns) FROM files GROUP BY provider, decision"
    ):
        result = providers[provider]
        result["files_on_disk"] += count
        if decision in counts:
            result[counts[decision]] = count
        else:
            result["ignored"][decision] = count
        oldest = _timestamp(oldest)
        if result["oldest_mtime"] is None or (oldest and oldest < result["oldest_mtime"]):
            result["oldest_mtime"] = oldest
    return providers


def report(data: Path, *, enabled: bool) -> dict:
    try:
        providers = json.loads((data / "scan-status.json").read_bytes())
    except FileNotFoundError:
        providers = {}
    return {"enabled": enabled, "providers": providers}


class Scanner:
    def __init__(self, paths: UserPaths):
        self.paths = paths
        self._settings: Scan | None = None
        self._walk: Generator[_File | _Unreadable | None] | None = None
        self._pending: deque[_File] = deque()
        self._retried: set[_File] = set()
        self._unreadable: dict[str, int] = {}
        self._checked_at: str | None = None
        self._next_cycle = float("-inf")

    def due(self, settings: Scan, now: float) -> bool:
        return settings.enabled and (settings != self._settings or now >= self._next_cycle)

    def tick(
        self,
        config: Config,
        register: _Register,
        ignored: IgnoredRecords,
        *,
        clock: Callable[[], float] = time.monotonic,
        capture_budget: float = TICK_BUDGET,
    ) -> bool:
        settings = config.scan
        if settings != self._settings:
            if self._walk is not None:
                self._walk.close()
            self._settings = settings
            self._walk = None
            self._pending.clear()
            self._checked_at = None
            self._next_cycle = float("-inf")
        if not settings.enabled or clock() < self._next_cycle:
            return False
        if self._checked_at is None:
            self._checked_at = datetime.now(UTC).isoformat()
            self._walk = _provider_files(settings)
            self._retried.clear()
            self._unreadable = dict.fromkeys(_PROVIDERS, 0)
        deadline = clock() + WALK_SECONDS
        while self._walk is not None and len(self._pending) < QUEUE_LIMIT and clock() < deadline:
            try:
                file = next(self._walk)
            except StopIteration:
                self._walk = None
            else:
                if isinstance(file, _Unreadable):
                    self._unreadable[file.provider] += 1
                elif file is not None:
                    self._pending.append(file)

        self.paths.data.mkdir(parents=True, exist_ok=True)
        deadline = clock() + capture_budget
        pending = self._pending.copy()
        deferred = []
        routes: dict[str | None, tuple[Config, Resolution | ScanIgnoreReason]] = {}
        with closing(sqlite3.connect(self.paths.data / "scan.sqlite3", timeout=0.1)) as db:
            db.row_factory = sqlite3.Row
            db.execute(_SCHEMA)
            with db:
                while pending and clock() < deadline:
                    file = pending[0]
                    try:
                        more = self._process(db, config, register, ignored, file, routes)
                    except ConfigError, WriterBusy:
                        deferred.append(file)
                        more = False
                    pending.popleft()
                    if more:
                        pending.append(file)
                pending.extend(deferred)
                active = self._walk is not None or bool(pending)
                if not active:
                    db.execute("DELETE FROM files WHERE checked_at != ?", (self._checked_at,))
                    providers = _coverage(db, datetime.now(UTC).isoformat(), self._unreadable)
        self._pending = pending
        self._retried.update(deferred)
        if active:
            return True
        atomic_write(self.paths.data / "scan-status.json", json.dumps(providers).encode())
        self._checked_at = None
        self._next_cycle = clock() + settings.interval_seconds
        return False

    def _process(
        self,
        db: sqlite3.Connection,
        config: Config,
        register: _Register,
        ignored: IgnoredRecords,
        file: _File,
        routes: dict[str | None, tuple[Config, Resolution | ScanIgnoreReason]],
    ) -> bool:
        name = str(file.path)
        try:
            info = file.path.stat()
        except FileNotFoundError:
            return False
        previous = db.execute("SELECT * FROM files WHERE path=?", (name,)).fetchone()
        unchanged = previous is not None and (
            previous["provider"],
            previous["size"],
            previous["mtime_ns"],
        ) == (file.provider, info.st_size, info.st_mtime_ns)
        if (
            unchanged
            and previous["decision"] == "complete"
            and any(str(project.id) == previous["project_id"] for project in config.projects)
        ):
            db.execute("UPDATE files SET checked_at=? WHERE path=?", (self._checked_at, name))
            return False

        identity, side_directory = file.sources()
        project_id = None
        more = False
        if identity is None:
            decision = "unrecognized"
        else:
            try:
                cwd = _source_cwd(file, identity[1], side_directory)
                if cwd not in routes:
                    routes[cwd] = self._route(config, register, cwd)
                config, target = routes[cwd]
                ignore_reason: ScanIgnoreReason | None = None
                if isinstance(target, Resolution):
                    project_id = str(target.project_id)
                    project = next(item for item in config.projects if item.id == target.project_id)
                    limits = project.overrides.apply(config.defaults)
                    if limits.capture_content:
                        source = Source(file.provider, *identity, file.path, None, "scan", cwd)
                        progressed, complete = capture_file(
                            self.paths.project_data(target.project_id),
                            limits,
                            source,
                            side_directory=side_directory,
                        )
                        current = file.path.stat()
                        stable = (info.st_size, info.st_mtime_ns) == (
                            current.st_size,
                            current.st_mtime_ns,
                        )
                        decision = "complete" if complete and stable else "partial"
                        more = progressed and stable and not complete
                    else:
                        ignore_reason = decision = "capture_disabled"
                else:
                    ignore_reason = decision = target
                if ignore_reason is not None and (
                    not unchanged or previous["decision"] != decision
                ):
                    ignored.record_scan(cwd or str(file.path.parent), ignore_reason)
            except ConfigError, WriterBusy:
                if file not in self._retried:
                    raise
                decision = "partial"
            except (
                OSError,
                EOFError,
                zstd.ZstdError,
                StorageError,
                UnsupportedTranscript,
                sqlite3.Error,
            ):
                decision = "partial"
        db.execute(
            _DECISIONS,
            (
                file.provider,
                name,
                info.st_size,
                info.st_mtime_ns,
                project_id,
                decision,
                self._checked_at,
            ),
        )
        return more

    def _route(
        self, config: Config, register: _Register, cwd: str | None
    ) -> tuple[Config, Resolution | ScanIgnoreReason]:
        if cwd is None:
            return config, "no_cwd"
        try:
            path = _existing_directory(cwd)
            target = Registry(config).resolve(path, timeout=0.25)
            if target is not None:
                return config, target
            return register(self.paths, path, timeout=0.25)
        except OSError, RegistryError:
            return config, "unresolvable_cwd"
