import builtins
import io
import json
import os
import subprocess
import tempfile
from compression import zstd
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest

from agent_watchdog import daemon, inspection
from agent_watchdog.capture import capture
from agent_watchdog.capture_store import CAPTURE_FILENAME, CaptureStore, iter_lines
from agent_watchdog.config import Config, Project, UserPaths, load_config, save_config
from agent_watchdog.events import Envelope
from agent_watchdog.ignored import IgnoredRecords
from agent_watchdog.ignored import report as ignored_report
from agent_watchdog.storage import Store


@pytest.fixture
def scan_config(tmp_path):
    return lambda: setup_scan(tmp_path)


def setup_scan(tmp_path):
    from agent_watchdog.config import Scan

    state = tmp_path / "watchdog"
    paths = UserPaths(state / "config.toml", state / "data", state / "runtime")
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    archived = tmp_path / "archived"
    checkout = tmp_path / "project"
    checkout.mkdir()
    project = Project(id=uuid4(), root=checkout)
    config = Config(
        projects=(project,),
        scan=Scan(
            enabled=True,
            claude_roots=(claude,),
            codex_roots=(codex, archived),
            interval_seconds=300,
        ),
    )
    save_config(paths.config, config)
    return paths, config, project, claude, codex, archived


def make_scanner(paths):
    from agent_watchdog.scan import Scanner

    return Scanner(paths)


def finish_cycle(scanner, config, ignored, clock=lambda: 100.0):
    for _ in range(80):
        if not scanner.tick(config, daemon.resolve_or_auto_register, ignored, clock=clock):
            return
    pytest.fail("The synthetic scan cycle did not finish")


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def codex_rollout(directory, cwd, *, compressed=False, trailing=b""):
    session = str(uuid4())
    suffix = ".jsonl.zst" if compressed else ".jsonl"
    path = directory / f"rollout-2026-11-12T00-00-00-{session}{suffix}"
    payload = {"id": session}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    data = (
        json.dumps({"type": "session_meta", "payload": payload}).encode()
        + b'\n{"type":"response_item"}\n'
        + trailing
    )
    write(path, zstd.compress(data) if compressed else data)
    return path, session, data


def captured(paths, project):
    with CaptureStore(paths.project_data(project.id) / CAPTURE_FILENAME).connect() as db:
        return [
            (dict(row), list(iter_lines(db, row["stream_pk"])))
            for row in db.execute("SELECT * FROM streams ORDER BY stream_pk")
        ]


@pytest.mark.parametrize("location", ["plain", "compressed", "archived"])
def test_scan_backfills_codex_rollout(scan_config, location):
    paths, config, project, _, codex, archived = scan_config()
    root = archived if location == "archived" else codex / "2026" / "11" / "12"
    _, session, data = codex_rollout(root, project.root, compressed=location == "compressed")

    finish_cycle(make_scanner(paths), config, IgnoredRecords(paths.data))

    ((stream, lines),) = captured(paths, project)
    assert (stream["provider"], stream["session_id"], stream["stream_id"]) == (
        "codex",
        session,
        "main",
    )
    assert stream["cwd"] == str(project.root)
    assert stream["source_kind"] == "scan"
    assert lines == data.splitlines()


def test_scan_backfills_claude_session_family(scan_config):
    paths, config, project, claude, _, _ = scan_config()
    session = str(uuid4())
    slug = claude / "project-slug"
    main = b"{}\n" + json.dumps({"cwd": str(project.root)}).encode() + b"\n"
    child = b'{"type":"assistant"}\n'
    write(slug / f"{session}.jsonl", main)
    write(slug / session / "subagents" / "agent-a12f.jsonl", child)
    write(slug / session / "tool-results" / "output", b"side content")

    finish_cycle(make_scanner(paths), config, IgnoredRecords(paths.data))

    assert {
        (row["provider"], row["session_id"], row["stream_id"], row["source_kind"], tuple(lines))
        for row, lines in captured(paths, project)
    } == {
        ("claude", session, "main", "scan", tuple(main.splitlines())),
        ("claude", session, "a12f", "scan", tuple(child.splitlines())),
    }
    with CaptureStore(paths.project_data(project.id) / CAPTURE_FILENAME).connect() as db:
        ((provider, session_id, path, data),) = db.execute(
            "SELECT provider, session_id, rel_path, data FROM side_files"
        )
    assert (provider, session_id, path, zstd.decompress(data)) == (
        "claude",
        session,
        "tool-results/output",
        b"side content",
    )


def test_scan_registers_nearest_git_ancestor_of_vanished_cwd(scan_config, tmp_path, monkeypatch):
    paths, config, project, _, codex, _ = scan_config()
    repo = tmp_path / "another-project"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init"], check=True, capture_output=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "scratch"))
    vanished = repo / "removed" / "directory"
    _, session, _ = codex_rollout(codex, vanished)

    finish_cycle(make_scanner(paths), config, IgnoredRecords(paths.data))

    original, registered = load_config(paths.config).projects
    assert original == project.model_copy(update={"alias": project.root.name})
    assert registered.root == repo.resolve()
    assert registered.alias == repo.name
    ((stream, _),) = captured(paths, registered)
    assert (stream["session_id"], stream["cwd"]) == (session, str(vanished))


@pytest.mark.parametrize("reason", ["no_cwd", "not_git", "excluded", "unresolvable_cwd"])
def test_scan_counts_ignored_rollout_without_removing_it(scan_config, tmp_path, reason):
    paths, config, project, _, codex, _ = scan_config()
    cwd = None
    if reason in {"not_git", "excluded"}:
        cwd = tmp_path / "unregistered"
        cwd.mkdir()
    if reason == "excluded":
        subprocess.run(["git", "-C", str(cwd), "init"], check=True, capture_output=True)
        config = config.model_copy(update={"collect_exclude": ("*/unregistered",)})
        save_config(paths.config, config)
    if reason == "unresolvable_cwd":
        cwd = str(tmp_path / "invalid") + "\0"
    path, _, data = codex_rollout(codex, cwd)
    ignored = IgnoredRecords(paths.data)

    finish_cycle(make_scanner(paths), config, ignored)
    ignored.flush()

    assert ignored_report(paths.data)["by_reason"] == {reason: 1}
    assert load_config(paths.config).projects == (project,)
    assert path.read_bytes() == data


def test_scan_does_not_open_unchanged_fully_captured_file_after_restart(scan_config, monkeypatch):
    paths, config, project, _, codex, _ = scan_config()
    path, _, data = codex_rollout(codex, project.root)
    ignored = IgnoredRecords(paths.data)
    finish_cycle(make_scanner(paths), config, ignored)
    opened = []

    def record_open(original):
        def open_file(file, *args, **kwargs):
            if not isinstance(file, int) and Path(file) == path:
                opened.append(file)
            return original(file, *args, **kwargs)

        return open_file

    monkeypatch.setattr(io, "open", record_open(io.open))
    monkeypatch.setattr(builtins, "open", record_open(builtins.open))
    finish_cycle(make_scanner(paths), config, ignored, clock=lambda: 401.0)

    assert not opened
    assert [lines for _, lines in captured(paths, project)] == [data.splitlines()]


def test_scan_yields_during_directory_walk_and_resumes(scan_config, monkeypatch):
    paths, config, project, _, codex, _ = scan_config()
    sessions = {codex_rollout(codex / "2026", project.root)[1] for _ in range(8)}
    original_scandir = os.scandir
    elapsed = 100.0
    visited = []

    class SlowScandir:
        def __init__(self, directory):
            self.entries = original_scandir(directory)

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal elapsed
            entry = next(self.entries)
            elapsed += 0.03
            if entry.is_file():
                visited.append(entry.name)
            return entry

        def close(self):
            self.entries.close()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def scandir(directory):
        if isinstance(directory, (str, Path)) and Path(directory).is_relative_to(codex):
            return SlowScandir(directory)
        return original_scandir(directory)

    def clock():
        return elapsed

    monkeypatch.setattr(os, "scandir", scandir)
    scanner = make_scanner(paths)
    ignored = IgnoredRecords(paths.data)

    assert scanner.tick(config, daemon.resolve_or_auto_register, ignored, clock=clock)
    assert len(visited) < len(sessions)
    finish_cycle(scanner, config, ignored, clock)

    assert len(visited) == len(sessions)
    assert {row["session_id"] for row, _ in captured(paths, project)} == sessions


def test_scan_deduplicates_a_stream_already_seen_by_hook(scan_config):
    paths, config, project, _, codex, _ = scan_config()
    path, session, data = codex_rollout(codex, project.root)
    with Store(paths.project_data(project.id), project.id) as store:
        store.put(
            Envelope(
                provider="codex",
                project_id=project.id,
                kind="turn.start",
                source="hook",
                session_id=session,
                payload={"codex": {"transcript_path": str(path)}},
            )
        )
        capture(store, budget_seconds=10)

    finish_cycle(make_scanner(paths), config, IgnoredRecords(paths.data))

    ((stream, lines),) = captured(paths, project)
    assert stream["source_kind"] == "hook"
    assert lines == data.splitlines()


def test_scan_continues_after_an_unreadable_directory(scan_config, monkeypatch):
    paths, config, project, claude, codex, _ = scan_config()
    blocked = claude / "blocked"
    blocked.mkdir(parents=True)
    _, session, _ = codex_rollout(codex, project.root)
    scandir = os.scandir

    def open_directory(directory):
        if isinstance(directory, (str, Path)) and Path(directory) == blocked:
            raise PermissionError("Unreadable provider directory")
        return scandir(directory)

    monkeypatch.setattr(os, "scandir", open_directory)
    finish_cycle(make_scanner(paths), config, IgnoredRecords(paths.data))

    ((stream, _),) = captured(paths, project)
    assert stream["session_id"] == session
    report = inspection.doctor(paths, transcript_roots={})
    assert report["scan"]["providers"]["claude"]["unreadable_directories"] == 1
    assert "scan_directory_unreadable" in {warning["code"] for warning in report["warnings"]}


def test_scan_finishes_divergent_copies_of_the_same_rollout(scan_config, monkeypatch):
    paths, config, project, _, codex, archived = scan_config()
    first, session, raw = codex_rollout(codex, project.root)
    second = archived / first.name
    first.write_bytes(raw + b'{"copy":"a"}\n' * 70)
    write(second, raw + b'{"copy":"b"}\n' * 70)
    monkeypatch.setattr("agent_watchdog.transcripts.READ_BYTES", 512)
    elapsed = 100.0

    def clock():
        nonlocal elapsed
        elapsed += 0.005
        return elapsed

    finish_cycle(make_scanner(paths), config, IgnoredRecords(paths.data), clock)

    rows = captured(paths, project)
    assert len(rows) == 2
    assert {row["session_id"] for row, _ in rows} == {session}
    assert {b"\n".join(lines) + b"\n" for _, lines in rows} == {
        first.read_bytes(),
        second.read_bytes(),
    }


def test_doctor_reports_scan_coverage(scan_config, monkeypatch):
    paths, config, project, _, codex, _ = scan_config()
    complete, _, _ = codex_rollout(codex, project.root)
    codex_rollout(codex, project.root, trailing=b"partial")
    codex_rollout(codex, None)
    write(codex / "rollout-invalid.jsonl", b"{}\n")
    oldest = 1_700_000_000
    os.utime(complete, (oldest, oldest))
    monkeypatch.setattr(daemon, "alive", lambda _: True)
    monkeypatch.setattr(daemon, "status", lambda _: {"state": "ok"})

    finish_cycle(make_scanner(paths), config, IgnoredRecords(paths.data))
    coverage = inspection.doctor(paths, transcript_roots={})["scan"]["providers"]["codex"]

    assert coverage["files_on_disk"] == 4
    assert coverage["fully_captured"] == 1
    assert coverage["partly_captured"] == 1
    assert coverage["ignored"] == {"no_cwd": 1}
    assert coverage["unrecognized_names"] == 1
    assert datetime.fromisoformat(coverage["last_finished_at"]).tzinfo is not None
    assert datetime.fromisoformat(coverage["oldest_mtime"]).timestamp() == oldest


def test_doctor_warns_when_scan_is_disabled(tmp_path, monkeypatch):
    paths = UserPaths(tmp_path / "config.toml", tmp_path / "data", tmp_path / "runtime")
    save_config(paths.config, Config())
    monkeypatch.setattr(daemon, "alive", lambda _: True)
    monkeypatch.setattr(daemon, "status", lambda _: {"state": "ok"})

    report = inspection.doctor(paths, transcript_roots={})

    assert report["scan"]["enabled"] is False
    assert "scan_disabled" in {warning["code"] for warning in report["warnings"]}
