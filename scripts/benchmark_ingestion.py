"""Opt-in spool-to-SQLite benchmark with isolated loopback control requests; no providers."""

from __future__ import annotations

import argparse
import cProfile
import json
import platform
import pstats
import socket
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from agent_watchdog import resources
from agent_watchdog.config import Config, Project, UserPaths, save_config
from agent_watchdog.daemon import _drain_spool, _policy_server
from agent_watchdog.registry import Registry, discover
from agent_watchdog.storage import DrainResult, Store


def _project_id(source_db: Path) -> UUID:
    with closing(sqlite3.connect(f"file:{source_db.as_posix()}?mode=ro", uri=True)) as db:
        rows = db.execute("SELECT project_id FROM metadata").fetchall()
    if len(rows) != 1:
        raise ValueError("Source database has no unique project identity")
    return UUID(rows[0][0])


def _backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)) as original:
        with closing(sqlite3.connect(destination)) as copy:
            original.backup(copy)


def _record(checkout: Path, index: int) -> dict:
    now = datetime.now(UTC).isoformat()
    return {
        "schema_version": 2,
        "record_type": "hook",
        "event_id": str(uuid4()),
        "received_at": now,
        "provider": "codex",
        "cwd": str(checkout),
        "input": {
            "hook_event_name": "PostToolUse",
            "session_id": "benchmark-session",
            "tool_name": "Bash",
            "tool_input": {"command": f"printf benchmark-{index}"},
            "tool_response": {"stdout": "synthetic", "exit_code": 0},
        },
        "delivery": {},
    }


def _timed[**P, R](
    target: Callable[P, R], label: str, costs: dict[str, float], counts: dict[str, int]
) -> Callable[P, R]:
    @wraps(target)
    def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.perf_counter()
        try:
            return target(*args, **kwargs)
        finally:
            costs[label] = costs.get(label, 0.0) + time.perf_counter() - started
            counts[label] = counts.get(label, 0) + 1

    return invoke


def _control_request(discovery: dict, checkout: Path) -> None:
    request = {
        "schema_version": 2,
        "token": discovery["token"],
        "provider": "claude",
        "event_id": str(uuid4()),
        "input": {
            "cwd": str(checkout),
            "session_id": "benchmark-control",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "printf benchmark-control"},
        },
    }
    deadline = time.monotonic() + 0.3
    with socket.create_connection(("127.0.0.1", discovery["port"]), timeout=0.3) as connection:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Control request exceeded its budget")
        connection.settimeout(remaining)
        connection.sendall((json.dumps(request) + "\n").encode())
        connection.shutdown(socket.SHUT_WR)
        chunks = bytearray()
        while len(chunks) < 65536:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Control request exceeded its budget")
            connection.settimeout(remaining)
            chunk = connection.recv(4096)
            if not chunk:
                break
            chunks.extend(chunk)
    response = json.loads(chunks)
    if response.get("schema_version") != 2 or response.get("action") != "allow":
        raise ValueError("Unexpected benchmark control decision")


def _percentile(samples: list[float], fraction: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    index = (len(ordered) - 1) * fraction
    low = int(index)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


@contextmanager
def _control_probe(paths: UserPaths, checkout: Path, *, enabled: bool) -> Iterator[dict | None]:
    if not enabled:
        yield None
        return
    samples: list[float] = []
    errors: dict[str, int] = {}
    lock = threading.Lock()
    report = {"clients": 4, "rate_per_client_hz": 5, "budget_ms": 300}
    stop = threading.Event()
    ready = threading.Barrier(5)
    with _policy_server(paths):
        discovery = json.loads((paths.data / "policy" / "socket.json").read_bytes())

        def client() -> None:
            ready.wait()
            while not stop.is_set():
                started = time.perf_counter()
                try:
                    _control_request(discovery, checkout)
                except (OSError, ValueError, KeyError) as error:
                    name = type(error).__name__
                    with lock:
                        errors[name] = errors.get(name, 0) + 1
                else:
                    with lock:
                        samples.append((time.perf_counter() - started) * 1000)
                stop.wait(max(0.0, 0.2 - (time.perf_counter() - started)))

        threads = [threading.Thread(target=client, daemon=True) for _ in range(4)]
        for thread in threads:
            thread.start()
        ready.wait()
        try:
            yield report
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=0.5)
    report.update(
        samples=len(samples),
        p50_ms=_percentile(samples, 0.5),
        p95_ms=_percentile(samples, 0.95),
        errors=sum(errors.values()),
        error_counts=errors,
    )


def _profile_stages(profiles: list[tuple[str, cProfile.Profile]]) -> dict[str, float | None]:
    values: dict[str, float | None] = {}
    needles = {
        "fact_projection": ("storage.py", "_project_fact"),
        "session_state_apply": ("storage.py", "_apply_state"),
        "session_state_flush": ("storage.py", "_flush_codex_state"),
        "session_replay": ("storage.py", "_replay_codex_state"),
    }
    for phase, profile in profiles:
        stats = pstats.Stats(profile)
        raw_stats = getattr(stats, "stats", {})
        read_seconds = 0.0
        read_seen = False
        for (filename, _line, function), (_cc, _nc, tt, _ct, _callers) in raw_stats.items():
            for label, (file_part, function_part) in needles.items():
                if file_part in filename and function_part == function:
                    values[f"{label}_exclusive"] = (values.get(f"{label}_exclusive") or 0.0) + tt
                    values[f"{label}_inclusive"] = (values.get(f"{label}_inclusive") or 0.0) + _ct
            if "<method 'read'" in function or "<built-in method read" in function:
                read_seconds += tt
                read_seen = True
        values[f"file_read_calls_{phase}_exclusive"] = read_seconds if read_seen else None
    return values


def _run_copy(
    source_db: Path,
    checkout: Path,
    parent: Path,
    events: int,
    batch_size: int,
    *,
    profile_stages: bool,
    records: list[dict] | None = None,
    measure_control: bool = False,
) -> dict:
    project_id = _project_id(source_db)
    data = parent / "state"
    project_root = checkout.resolve()
    project = Project(
        id=project_id,
        root=project_root,
        git_common_dir=discover(project_root).git_common_dir,
    )
    config = Config(projects=(project,))
    paths = UserPaths(parent / "config.toml", data, parent / "runtime")
    save_config(paths.config, config)
    root = paths.project_data(project_id)
    _backup(source_db, root / "events.sqlite3")
    spool = data / "spool"
    spool.mkdir(parents=True)
    records = (
        records
        if records is not None
        else [_record(project_root, index) for index in range(events)]
    )
    for index, record in enumerate(records[:events]):
        (spool / f"{index:08d}.json").write_text(json.dumps(record), encoding="utf-8")
    costs: dict[str, float] = {}
    counts: dict[str, int] = {}
    commit_calls: list[float] = []
    insert_calls: list[float] = []
    profile = cProfile.Profile() if profile_stages else None
    stats = DrainResult()
    with ExitStack() as scope:
        if profile_stages:
            import agent_watchdog.daemon as daemon
            import agent_watchdog.hooks as hooks

            targets = (
                (Registry, "resolve", "registry_resolution"),
                (Store, "put", "store_put"),
                (Store, "__enter__", "store_open"),
                (daemon, "_capture_diff_snapshot", "checkout_fingerprint"),
                (hooks, "build_envelope", "envelope_build"),
                (resources, "available", "quota_availability_scan"),
            )
            for owner, attribute, label in targets:
                scope.enter_context(
                    patch.object(
                        owner, attribute, _timed(getattr(owner, attribute), label, costs, counts)
                    )
                )
            original_connect = sqlite3.connect

            class ProfiledConnection(sqlite3.Connection):
                def execute(self, sql, parameters=(), /):
                    started_at = time.perf_counter()
                    try:
                        return super().execute(sql, parameters)
                    finally:
                        elapsed = time.perf_counter() - started_at
                        statement = sql.strip().upper()
                        if statement == "COMMIT":
                            commit_calls.append(elapsed)
                        elif statement.startswith("INSERT"):
                            insert_calls.append(elapsed)

            def profiled_connect(*args, **kwargs):
                kwargs["factory"] = ProfiledConnection
                return original_connect(*args, **kwargs)

            scope.enter_context(patch.object(sqlite3, "connect", profiled_connect))
        control = scope.enter_context(_control_probe(paths, project_root, enabled=measure_control))
        if profile is not None:
            profile.enable()
        started = time.perf_counter()
        try:
            while stats.inserted + stats.duplicates < events:
                before = stats.inserted + stats.duplicates
                activity = _drain_spool(
                    paths, config, limit=100, batch_size=batch_size, stats=stats
                )
                if stats.quarantined or stats.discarded:
                    raise RuntimeError("Synthetic records were rejected during ingestion")
                if not activity or stats.inserted + stats.duplicates == before:
                    raise RuntimeError("Ingestion stalled before every synthetic event was stored")
        finally:
            seconds = time.perf_counter() - started
            if profile is not None:
                profile.disable()
    if profile is not None:
        stage_profile = _profile_stages([("direct_drain", profile)])
        stage_profile.update(costs)
        stage_profile["sqlite_commit_including_sync"] = sum(commit_calls)
        stage_profile["sqlite_insert_execution"] = sum(insert_calls)
        counts["sqlite_commit"] = len(commit_calls)
        counts["sqlite_insert"] = len(insert_calls)
    else:
        stage_profile = None
    return {
        "batch_size": batch_size,
        "events_requested": events,
        "events_inserted": stats.inserted,
        "duplicates": stats.duplicates,
        "quarantined": stats.quarantined,
        "discarded": stats.discarded,
        "spool_seconds": seconds,
        "total_seconds": seconds,
        "events_per_minute": events / seconds * 60,
        "stage_profile_seconds": stage_profile,
        "stage_counts": counts if profile_stages else None,
        "control_latency": control,
    }


def run_benchmark(
    source_db: Path,
    checkout: Path,
    work_dir: Path,
    events: int,
    profile_events: int | None = None,
    *,
    measure_control: bool = True,
) -> dict:
    if events <= 0:
        raise ValueError("Event count must be positive")
    profile_events = min(events, 200) if profile_events is None else profile_events
    if profile_events <= 0 or profile_events > events:
        raise ValueError("Profile event count must be between one and the workload size")
    source_db = source_db.resolve(strict=True)
    checkout = checkout.resolve(strict=True)
    if source_db.name != "events.sqlite3" or not checkout.is_dir():
        raise ValueError("Expected an events.sqlite3 source and an existing checkout")
    work_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="seed-", dir=work_dir) as seed_directory:
        seed = Path(seed_directory) / "source.sqlite3"
        _backup(source_db, seed)
        seed_hash = _sha256(seed)
        seed_size = seed.stat().st_size
        with closing(sqlite3.connect(f"file:{seed.as_posix()}?mode=ro", uri=True)) as db:
            seed_event_count = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        results = []
        records = [_record(checkout, index) for index in range(events)]
        for batch_size in (1, 25, 50, 100):
            runs = {}
            with tempfile.TemporaryDirectory(
                prefix=f"batch-{batch_size}-", dir=work_dir
            ) as temporary:
                runs["throughput"] = _run_copy(
                    seed,
                    checkout,
                    Path(temporary) / "throughput",
                    events,
                    batch_size,
                    profile_stages=False,
                    records=records,
                    measure_control=measure_control,
                )
                runs["stage_profile"] = _run_copy(
                    seed,
                    checkout,
                    Path(temporary) / "profile",
                    profile_events,
                    batch_size,
                    profile_stages=True,
                    records=records,
                )
            if runs["throughput"]["events_inserted"] != events:
                raise RuntimeError("Synthetic throughput run did not insert every event")
            if runs["stage_profile"]["events_inserted"] != profile_events:
                raise RuntimeError("Synthetic profiling run did not insert every event")
            throughput = runs["throughput"]
            results.append(
                {
                    "batch_size": batch_size,
                    "events_requested": events,
                    "events_inserted": events,
                    "events_profiled": profile_events,
                    "duplicates": throughput["duplicates"],
                    "quarantined": throughput["quarantined"],
                    "discarded": throughput["discarded"],
                    "spool_seconds": throughput["spool_seconds"],
                    "total_seconds": throughput["total_seconds"],
                    "events_per_minute": throughput["events_per_minute"],
                    "profile_spool_seconds": runs["stage_profile"]["spool_seconds"],
                    "profile_total_seconds": runs["stage_profile"]["total_seconds"],
                    "stage_profile": runs["stage_profile"]["stage_profile_seconds"],
                    "stage_counts": runs["stage_profile"]["stage_counts"],
                    "control_latency": throughput["control_latency"],
                }
            )
    return {
        "benchmark": "synthetic_spool_to_sqlite",
        "events": events,
        "reader_window_limit": 100,
        "time_budget_seconds": None,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "seed_database_sha256": seed_hash,
        "seed_database_size_bytes": seed_size,
        "seed_event_count": seed_event_count,
        "profiling_note": (
            "Throughput is unprofiled. Stage timing uses separate identical copies; "
            "COMMIT timings include SQLite sync work. File read calls are measured per phase, "
            "including ancillary reads in that phase. Control latency uses four bounded-rate "
            "authenticated loopback clients during unprofiled ingestion only."
        ),
        "results": results,
    }


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db", type=Path, required=True, help="Explicit source events.sqlite3"
    )
    parser.add_argument("--checkout", type=Path, required=True, help="Registered project checkout")
    parser.add_argument("--work-dir", type=Path, default=Path("tests/local/wd172"))
    parser.add_argument("--events", type=int, default=2000)
    parser.add_argument(
        "--no-control-latency",
        action="store_true",
        help="Disable concurrent loopback decision requests",
    )
    parser.add_argument(
        "--profile-events",
        type=int,
        help="Instrument this many records per batch size (default: min(--events, 200))",
    )
    args = parser.parse_args()
    try:
        report = run_benchmark(
            args.source_db,
            args.checkout,
            args.work_dir,
            args.events,
            args.profile_events,
            measure_control=not args.no_control_latency,
        )
    except (OSError, ValueError, sqlite3.Error, RuntimeError):
        parser.error("Benchmark failed; source paths and data are withheld from output")
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
