"""Discover and import transcripts through the Watchdog read contract."""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from itertools import islice
from pathlib import Path

from agent_watchdog import readapi
from wdi.db.repository import Repository
from wdi.ingest.claude_code import parse_claude_lines
from wdi.ingest.codex import parse_codex_lines
from wdi.ingest.hook_facts import import_hook_facts
from wdi.ingest.models import MAIN_STREAM, ImportProgress, ImportStats
from wdi.ingest.projects import ProjectResolver
from wdi.ingest.runner import ParseFn, import_stream

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchdogSource:
    store: readapi.StoreInfo
    stream: readapi.StreamInfo

    @property
    def key(self) -> str:
        return (
            f"wd://{self.store.machine_id}/{self.store.project_id}/"
            f"{self.stream.provider}/{self.stream.session_id}/{self.stream.stream_id}"
        )

    # In import_state the file size, mtime and offset fields hold raw bytes,
    # generation and record count respectively for a Watchdog key.
    @property
    def size(self) -> int:
        return self.stream.raw_bytes

    @property
    def version(self) -> int:
        return self.stream.generation

    @property
    def record_count(self) -> int:
        return self.stream.record_count

    @property
    def fallback_root(self) -> str | None:
        return self.store.root

    def read_lines(self, offset: int) -> tuple[list[bytes], int]:
        # A writer may add records after discovery. Only import the prefix
        # represented by the generation, byte count and record count we saw.
        try:
            lines = list(
                islice(
                    readapi.lines(self.store, self.stream.stream_pk, offset),
                    self.record_count - offset,
                )
            )
        except sqlite3.Error as exc:
            raise ValueError("Cannot read the capture database") from exc
        if len(lines) != self.record_count - offset:
            raise ValueError("Capture stream shortened after discovery")
        return lines, self.record_count


@dataclass(frozen=True)
class StoreScan:
    data_dir: Path
    stores: list[readapi.StoreInfo]
    sources: list[WatchdogSource]
    warnings: list[str]


def scan_stores(data_dirs: list[Path]) -> Iterator[StoreScan]:
    for data_dir in data_dirs:
        sources = []
        warnings = []
        try:
            stores = readapi.stores(data_dir)
            known = {store.path for store in stores}
            for path in sorted((data_dir / "projects").glob("*")):
                if path.is_dir() and path not in known:
                    warnings.append(f"Missing store manifest for {path.name}")
        except (OSError, ValueError) as exc:
            yield StoreScan(data_dir, [], [], [f"Cannot discover stores: {exc}"])
            continue
        for store in stores:
            if not store.supported:
                warnings.append(
                    f"Unsupported store contract {store.contract} for {store.path.name}"
                )
                continue
            try:
                sources.extend(
                    WatchdogSource(store, stream)
                    for stream in list(readapi.streams(store))
                    if stream.provider in _PARSERS
                )
            except (OSError, ValueError, sqlite3.Error) as exc:
                warnings.append(f"Cannot list streams for {store.path.name}: {exc}")
        yield StoreScan(data_dir, stores, sources, warnings)


_PARSERS: dict[str, ParseFn] = {"claude": parse_claude_lines, "codex": parse_codex_lines}


def import_watchdog(
    repo: Repository,
    data_dirs: list[Path],
    aliases: dict[str, list[str]],
    *,
    full: bool,
    on_progress: Callable[[ImportProgress], None] | None,
) -> ImportStats:
    total = ImportStats()
    sources: dict[tuple[str, str, str], WatchdogSource] = {}
    resolvers: dict[str, ProjectResolver] = {}
    codex_machines: set[str] = set()
    for scan in scan_stores(data_dirs):
        for warning in scan.warnings:
            logger.warning("%s: %s", scan.data_dir, warning)
            total.warnings += 1
        for store in scan.stores:
            if not store.supported:
                continue
            try:
                import_hook_facts(repo, [store], full=full)
            except (OSError, ValueError, sqlite3.Error) as exc:
                logger.warning("Could not import hook events for %s: %s", store.path.name, exc)
                total.warnings += 1
        for source in scan.sources:
            stream = source.stream
            key = (stream.provider, stream.session_id, stream.stream_id)
            previous = sources.get(key)
            if previous is None or _prefer_source(source, previous):
                sources[key] = source

    if on_progress is not None:
        on_progress(ImportProgress("watchdog", 0, len(sources)))
    ordered = sorted(sources.values(), key=lambda source: source.stream.stream_id != MAIN_STREAM)
    for done, source in enumerate(ordered, start=1):
        store, stream = source.store, source.stream
        assert store.machine_id is not None
        machine_id = store.machine_id
        if machine_id not in resolvers:
            repo.ensure_machine(machine_id, hostname=store.hostname, os_name=store.os)
            resolvers[machine_id] = ProjectResolver(repo, machine_id, aliases)
        try:
            stats = import_stream(
                repo,
                resolvers[machine_id],
                machine_id,
                source,
                stream.session_id,
                stream.stream_id if stream.provider == "claude" else MAIN_STREAM,
                stream.provider,
                _PARSERS[stream.provider],
                watchdog=True,
                full=full,
            )
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("Could not import %s: %s", source.key, exc)
            total.warnings += 1
            stats = None
        if stats is None:
            total.files_seen += 1
        else:
            total.add(stats)
        if stream.provider == "codex":
            codex_machines.add(machine_id)
        if on_progress is not None:
            on_progress(ImportProgress("watchdog", done, len(sources)))
    for machine_id in sorted(codex_machines):
        total.subagent_runs += repo.sync_codex_subagent_runs(machine_id)
    return total


def _prefer_source(source: WatchdogSource, previous: WatchdogSource) -> bool:
    if source.key == previous.key:
        return (source.version, source.record_count) > (previous.version, previous.record_count)
    return source.record_count > previous.record_count
