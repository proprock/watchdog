"""Bounded diagnostics for spool records skipped by collection policy."""

import json
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal, TypedDict

from pydantic import TypeAdapter, ValidationError

from agent_watchdog.files import atomic_write

IgnoreReason = Literal["not_git", "excluded", "unresolvable_cwd"]
_MAX_DIRECTORIES = 500
_FLUSH_SECONDS = 60


class _Entry(TypedDict):
    reason: IgnoreReason
    first_at: str
    last_at: str
    records: int


_entries_adapter = TypeAdapter(dict[str, _Entry])


class _Acknowledgement(TypedDict):
    digest: str
    files: list[str]


_ack_adapter = TypeAdapter(_Acknowledgement)


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _parse(content: bytes | None) -> dict[str, _Entry]:
    if content is None:
        return {}
    try:
        return _entries_adapter.validate_json(content, strict=True)
    except ValidationError as error:
        raise OSError("Invalid ignored-record counters in ignored.json") from error


def report(data: Path) -> dict:
    entries = _parse(_read_bytes(data / "ignored.json"))
    by_reason: dict[str, int] = {}
    for entry in entries.values():
        reason = entry["reason"]
        by_reason[reason] = by_reason.get(reason, 0) + entry["records"]
    recent = sorted(reversed(entries), key=lambda key: entries[key]["last_at"], reverse=True)
    return {
        "total": sum(by_reason.values()),
        "by_reason": by_reason,
        "recent": [{"directory": key, **entries[key]} for key in recent[:5]],
    }


class IgnoredRecords:
    def __init__(self, data: Path) -> None:
        self._snapshot = data / "ignored.json"
        self._journal = data / "ignored-ack.json"
        self._spool = data / "spool"
        self._entries: dict[str, _Entry] | None = None
        self._pending: set[Path] = set()
        self._written: set[Path] = set()
        self._warned: set[tuple[str, IgnoreReason]] = set()
        self._last_write = float("-inf")

    def _load(self) -> None:
        if self._entries is not None:
            return
        snapshot = _read_bytes(self._snapshot)
        entries = _parse(snapshot)
        journal = _read_bytes(self._journal)
        if journal is not None:
            try:
                ack = _ack_adapter.validate_json(journal, strict=True)
            except ValidationError as error:
                raise OSError("Invalid ignored-record acknowledgements") from error
            if snapshot is not None and sha256(snapshot).hexdigest() == ack["digest"]:
                if any(
                    not name.endswith(".json") or Path(name).name != name or name == "limits.json"
                    for name in ack["files"]
                ):
                    raise OSError("Invalid spool filename in ignored-record acknowledgements")
                self._written.update(self._spool / name for name in ack["files"])
        if snapshot is not None:
            age = max(0, time.time() - self._snapshot.stat().st_mtime)
            self._last_write = time.monotonic() - age
        self._entries = entries

    def __contains__(self, path: Path) -> bool:
        return path in self._pending or path in self._written

    def recover(self) -> None:
        if self._entries is None and self._journal.exists():
            self._load()

    def record(self, path: Path, directory: str, reason: IgnoreReason) -> bool:
        """Count one queued record and indicate whether its warning is new."""
        self._load()
        if path in self:
            return False
        assert self._entries is not None
        now = datetime.now(UTC).isoformat()
        previous = self._entries.pop(directory, None)
        self._entries[directory] = {
            "reason": reason,
            "first_at": previous["first_at"] if previous else now,
            "last_at": now,
            "records": previous["records"] + 1 if previous else 1,
        }
        while len(self._entries) > _MAX_DIRECTORIES:
            del self._entries[next(iter(self._entries))]
        self._pending.add(path)
        key = (directory, reason)
        if key in self._warned:
            return False
        self._warned.add(key)
        return True

    def flush(self, *, now: float | None = None) -> bool:
        if self._entries is None:
            return False
        acknowledged = self._acknowledge()
        now = time.monotonic() if now is None else now
        if self._pending and now - self._last_write >= _FLUSH_SECONDS:
            snapshot = json.dumps(self._entries).encode("utf-8")
            ack = {
                "digest": sha256(snapshot).hexdigest(),
                "files": sorted(path.name for path in self._pending),
            }
            # The digest distinguishes a committed snapshot from an interrupted write.
            atomic_write(self._journal, json.dumps(ack).encode("utf-8"))
            atomic_write(self._snapshot, snapshot)
            self._last_write = now
            self._written.update(self._pending)
            self._pending.clear()
            acknowledged |= self._acknowledge()
        return acknowledged

    def _acknowledge(self) -> bool:
        acknowledged = False
        for path in tuple(self._written):
            path.unlink(missing_ok=True)
            self._written.remove(path)
            acknowledged = True
        if self._journal.exists():
            self._journal.unlink(missing_ok=True)
        return acknowledged
