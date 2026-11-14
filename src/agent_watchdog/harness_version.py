"""Compare control profiles with bounded captured session metadata."""

import json
import re
import sqlite3
from collections.abc import Iterable
from compression import zstd
from pathlib import Path

from agent_watchdog.capture_store import CaptureStore
from agent_watchdog.events import Envelope
from agent_watchdog.storage import StorageError

_VERSION = re.compile(r"(\d+\.\d+\.\d+(?:-alpha\.\d+\.\d+)?)$")
_MAX_RECORD = 64 * 1024
_MAX_COMPRESSED = 128 * 1024


def _metadata_version(db: sqlite3.Connection, session: str, path: str) -> str | None:
    source = str(Path(path).with_suffix("")) if path.endswith(".zst") else path
    row = db.execute(
        "SELECT stream_pk, session_id FROM streams WHERE provider='codex' AND stream_id='main' "
        "AND hook_session_id=? AND source_path=? ORDER BY generation DESC LIMIT 1",
        (session, source),
    ).fetchone()
    if row is None:
        return None
    stream, stream_session = row
    chunk = db.execute(
        "SELECT codec, substr(data, 1, ?) FROM chunks WHERE stream_pk=? AND first_record=0 "
        "ORDER BY chunk_seq LIMIT 1",
        (_MAX_COMPRESSED, stream),
    ).fetchone()
    if chunk is not None:
        if chunk[0] != "zstd":
            return None
        raw = zstd.ZstdDecompressor().decompress(chunk[1], max_length=_MAX_RECORD + 1)
        if b"\n" not in raw:
            return None
        line = raw.split(b"\n", 1)[0]
    else:
        tail = db.execute(
            "SELECT substr(line, 1, ?) FROM tail WHERE stream_pk=? AND record_seq=0",
            (_MAX_RECORD + 1, stream),
        ).fetchone()
        if tail is None:
            return None
        line = tail[0]
    if len(line) > _MAX_RECORD:
        return None
    data = json.loads(line)
    if not isinstance(data, dict) or data.get("type") != "session_meta":
        return None
    payload = data.get("payload")
    if not isinstance(payload, dict) or payload.get("id") != stream_session:
        return None
    version = payload.get("cli_version")
    return version if isinstance(version, str) and version else None


def compare(path: Path, event: Envelope) -> tuple[str, str | None, str | None]:
    """Return match, drift or unknown; captured bytes are data only."""
    profile = event.delivery.get("codex_control_profile")
    match = _VERSION.search(profile) if isinstance(profile, str) else None
    payload = event.payload.get("codex")
    source = payload.get("transcript_path") if isinstance(payload, dict) else None
    if match is None or not isinstance(source, str) or not event.session_id:
        return "unknown", None, None
    expected = match.group(1)
    try:
        with CaptureStore(path).connect(write=False) as db:
            observed = _metadata_version(db, event.session_id, source)
    except OSError, sqlite3.Error, StorageError, ValueError, EOFError, zstd.ZstdError:
        observed = None
    if observed is None:
        return "unknown", expected, None
    return ("match" if observed == expected else "drift"), expected, observed


def add_findings(report: dict, path: Path, events: Iterable[Envelope]) -> None:
    """Add one finding per captured stream/profile, preserving hook evidence."""
    from agent_watchdog.analysis import _finding

    seen: set[tuple[str | None, object, object]] = set()
    for event in events:
        profile = event.delivery.get("codex_control_profile")
        if event.provider != "codex" or event.source != "hook" or not profile:
            continue
        payload = event.payload.get("codex")
        source = payload.get("transcript_path") if isinstance(payload, dict) else None
        if not isinstance(profile, str) or not isinstance(source, str):
            if "harness_version_unknown" not in report["gaps"]:
                report["gaps"].append("harness_version_unknown")
            continue
        key = (event.session_id, profile, source)
        if key in seen:
            continue
        seen.add(key)
        state, expected, observed = compare(path, event)
        if state == "unknown":
            if "harness_version_unknown" not in report["gaps"]:
                report["gaps"].append("harness_version_unknown")
        elif state == "drift":
            report["findings"].append(
                _finding(
                    "harness_version_drift",
                    [str(event.event_id)],
                    f"Profile backend {expected} differs from {observed}; "
                    "mechanics may have changed.",
                )
            )
