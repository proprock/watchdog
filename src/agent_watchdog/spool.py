"""The bounded durable input queue shared by all event publishers."""

import json
import stat
import time
from collections.abc import Callable, Iterator
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from agent_watchdog import resources
from agent_watchdog.config import Limits
from agent_watchdog.events import Envelope
from agent_watchdog.files import atomic_write
from agent_watchdog.storage import (
    DrainResult,
    MaintenanceBudget,
    QuotaExceeded,
    RejectedEvent,
    StorageError,
    Store,
    WriterBusy,
    canonical,
)

SCHEMA_VERSION = 2
SPOOL_BYTES = 64 * 1024**2
SPOOL_FILES = 4096
RECORD_OVERHEAD = 64 * 1024
MISMATCH_FILE = "schema-mismatch.json"


class Spool:
    def __init__(
        self,
        root: Path,
        *,
        limits: Limits | None = None,
        payload_bytes: int = 1024**2,
        byte_limit: int = SPOOL_BYTES,
        file_limit: int = SPOOL_FILES,
        quarantine_files: int = 128,
        quarantine_bytes: int = 8 * 1024**2,
    ) -> None:
        self.root = root
        self.directory = root / "spool"
        self.limits = limits or Limits(payload_bytes=payload_bytes)
        self.payload_bytes = self.limits.payload_bytes
        self.byte_limit = byte_limit
        self.file_limit = file_limit
        self.quarantine_files = quarantine_files
        self.quarantine_bytes = quarantine_bytes
        self._temporary: Iterator[Path] | None = None
        self._records: Iterator[Path] | None = None
        if min(self.payload_bytes, byte_limit, file_limit, quarantine_files, quarantine_bytes) <= 0:
            raise StorageError("Spool limits must be positive")

    def footprint(self) -> tuple[int, int]:
        if self.directory.is_symlink() or self.directory.is_junction():
            raise StorageError("Linked spool directory")
        files = size = 0
        for path in self.directory.iterdir() if self.directory.exists() else ():
            if path.name == "limits.json" or path.suffix not in {".json", ".tmp"}:
                continue
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISREG(metadata.st_mode):
                files += 1
                size += metadata.st_size
        return files, size

    def maintain(self, budget: MaintenanceBudget) -> bool:
        """Reclaim only old unpublished queue temporaries within the shared tick budget."""
        started = time.monotonic()
        deadline = started + max(0.0, budget.remaining)
        try:
            if started >= deadline:
                return True
            if self.directory.is_symlink() or self.directory.is_junction():
                raise StorageError("Linked spool directory")
            with resources.admission(self.root):
                if self._temporary is None:
                    self._temporary = self.directory.glob("*.tmp")
                cutoff = datetime.now(UTC).timestamp() - 3600
                while time.monotonic() < deadline:
                    path = next(self._temporary, None)
                    if path is None:
                        self._temporary = None
                        return False
                    try:
                        metadata = path.lstat()
                        if stat.S_ISREG(metadata.st_mode) and metadata.st_mtime < cutoff:
                            path.unlink()
                    except FileNotFoundError:
                        pass
        except (OSError, StorageError):
            return True
        finally:
            budget.remaining = max(0.0, budget.remaining - (time.monotonic() - started))
        return True

    def publish(self, event: Envelope, *, telemetry: bool = False) -> Path:
        if len(canonical(event).encode()) > self.payload_bytes:
            resources.count_loss(self.root, "payload")
            raise RejectedEvent("Envelope exceeds payload limit")
        record = {
            "schema_version": SCHEMA_VERSION,
            "record_type": "envelope",
            "envelope": event.model_dump(mode="json"),
        }
        return self.publish_record(record, telemetry=telemetry)

    def publish_record(self, record: dict, *, telemetry: bool = False) -> Path:
        """Serialize quota admission with the native publisher on admission.lock."""
        try:
            with resources.admission(self.root):
                files, size = self.footprint()
                record = dict(record)
                if telemetry:
                    record["delivery"] = dict(record.get("delivery", {})) | {
                        "spool_enqueued_at": datetime.now(UTC).isoformat(),
                        "spool_occupancy": {"files": files, "bytes": size},
                    }
                content = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
                if len(content) > self.payload_bytes + RECORD_OVERHEAD:
                    raise RejectedEvent("Spool record exceeds payload limit")
                if files + 1 > self.file_limit or size + len(content) > self.byte_limit:
                    raise QuotaExceeded("Spool quota reached")
                path = self.directory / f"{uuid4().hex}.json"
                atomic_write(path, content)
                return path
        except QuotaExceeded:
            resources.count_loss(self.root, "quota")
            raise
        except WriterBusy:
            resources.count_loss(self.root, "busy")
            raise
        except OSError:
            resources.count_loss(self.root, "io")
            raise

    def quarantine(self, path: Path, result: DrainResult) -> None:
        """Retain bounded diagnostics, never malformed provider content."""
        directory = self.root / "quarantine"
        if directory.is_symlink() or directory.is_junction():
            raise StorageError("Linked quarantine directory")
        with resources.admission(self.root):
            directory.mkdir(parents=True, exist_ok=True)
            diagnostic = json.dumps({"reason": "rejected", "bytes": path.lstat().st_size}).encode()
            entries = sorted(directory.glob("*.bad"), key=lambda item: item.stat().st_mtime_ns)
            total = sum(item.stat().st_size for item in entries)
            while entries and (
                len(entries) >= self.quarantine_files
                or total + len(diagnostic) > self.quarantine_bytes
            ):
                oldest = entries.pop(0)
                total -= oldest.stat().st_size
                oldest.unlink()
            if len(diagnostic) <= self.quarantine_bytes:
                atomic_write(directory / f"{uuid4().hex}.bad", diagnostic)
                result.quarantined += 1
            else:
                result.discarded += 1
            path.unlink()
        resources.count_loss(self.root, "invalid")

    def _note_schema_mismatch(self, seen: int) -> None:
        """Keep a per-version tally: a stale adapter would otherwise only inflate ``invalid``."""
        report = self.schema_mismatch() or {"seen": {}}
        tally = report["seen"]
        tally[str(seen)] = tally.get(str(seen), 0) + 1
        document = {
            "expected": SCHEMA_VERSION,
            "seen": tally,
            "last_at": datetime.now(UTC).isoformat(),
        }
        try:
            atomic_write(self.root / MISMATCH_FILE, json.dumps(document).encode())
        except OSError:
            pass  # Diagnostics must not stop the drain; the invalid counter still records the loss.

    def schema_mismatch(self) -> dict | None:
        """Return the cumulative record-schema mismatch report, or None if never observed."""
        try:
            document = json.loads((self.root / MISMATCH_FILE).read_bytes())
        except (OSError, ValueError):
            return None
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("seen"), dict)
            or not isinstance(document.get("last_at"), str)
        ):
            return None
        return document

    def records(
        self,
        result: DrainResult,
        *,
        limit: int = 100,
        on_error: Callable[[Exception], None] | None = None,
    ) -> Iterator[tuple[Path, dict]]:
        if self.directory.is_symlink() or self.directory.is_junction():
            raise StorageError("Linked spool directory")
        restarted = False
        for position in range(limit):
            if self._records is None:
                self._records = (
                    path for path in self.directory.glob("*.json") if path.name != "limits.json"
                )
            path = next(self._records, None)
            if path is None:
                self._records = None
                if position == 0 and not restarted:
                    restarted = True
                    self._records = (
                        path for path in self.directory.glob("*.json") if path.name != "limits.json"
                    )
                    path = next(self._records, None)
                if path is None:
                    self._records = None
                    break
            try:
                if path.is_symlink():
                    path.unlink()
                    result.discarded += 1
                    continue
                with path.open("rb") as stream:
                    data = stream.read(self.payload_bytes + RECORD_OVERHEAD + 1)
                if len(data) > self.payload_bytes + RECORD_OVERHEAD:
                    raise RejectedEvent("Spool record exceeds payload limit")
                record = json.loads(data)
                if (
                    isinstance(record, dict)
                    and type(record.get("schema_version")) is int
                    and record["schema_version"] != SCHEMA_VERSION
                ):
                    self._note_schema_mismatch(record["schema_version"])
                if (
                    not isinstance(record, dict)
                    or type(record.get("schema_version")) is not int
                    or record.get("schema_version") != SCHEMA_VERSION
                    or record.get("record_type") not in {"hook", "envelope"}
                ):
                    raise RejectedEvent("Invalid spool record")
                yield path, record
            except FileNotFoundError:
                continue
            except (ValueError, TypeError) as error:
                if on_error is not None:
                    on_error(error)
                try:
                    self.quarantine(path, result)
                except (OSError, StorageError) as retry:
                    if on_error is not None:
                        on_error(retry)
            except OSError as error:
                if on_error is not None:
                    on_error(error)

    def commit(
        self,
        store: Store,
        pending: list[tuple[Path, Envelope]],
        result: DrainResult,
        *,
        on_commit: Callable[[Envelope], None] | None = None,
        after_commit: Callable[[], None] | None = None,
    ) -> None:
        outcomes: list[bool | None] = []
        try:
            with store.batch() if len(pending) > 1 else nullcontext():
                for _, event in pending:
                    try:
                        outcomes.append(store.put(event))
                    except RejectedEvent:
                        outcomes.append(None)
        except QuotaExceeded:
            if len(pending) == 1:
                raise
            for item in pending:
                self.commit(store, [item], result, on_commit=on_commit, after_commit=after_commit)
            return
        if pending and after_commit is not None:
            after_commit()
        for (path, event), inserted in zip(pending, outcomes, strict=True):
            if inserted is None:
                self.quarantine(path, result)
                continue
            if inserted:
                result.inserted += 1
                if on_commit is not None:
                    on_commit(event)
            else:
                result.duplicates += 1
            # The outer transaction, state projection and receipts all committed.
            path.unlink()
