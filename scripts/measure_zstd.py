"""Measure zstd on local session files without invoking providers or writing data."""

import argparse
import statistics
import time
from collections.abc import Iterator
from compression import zstd
from dataclasses import dataclass, field
from pathlib import Path

MIB = 1024**2
LEVELS = (3, 6, 9, 12, 15, 19)
BLOCK_SIZES_MIB = (4, 1, 16)


@dataclass
class Measurement:
    stored_bytes: int = 0
    compress_seconds: float = 0.0
    decompress_seconds: float = 0.0
    full_block_ms: list[float] = field(default_factory=list)

    def add(self, block: bytes, block_size: int, level: int) -> None:
        started = time.perf_counter()
        packed = zstd.compress(block, level=level)
        compressed = time.perf_counter()
        restored = zstd.decompress(packed)
        decompressed = time.perf_counter()
        if restored != block:
            raise ValueError("A decompressed block differs from the original.")
        self.stored_bytes += len(packed)
        self.compress_seconds += compressed - started
        self.decompress_seconds += decompressed - compressed
        if len(block) == block_size:
            self.full_block_ms.append((compressed - started) * 1000)


def session_files(root: Path) -> list[Path]:
    paths = {
        path
        for pattern in ("*.jsonl", "*.jsonl.zst")
        for path in root.rglob(pattern)
        if not path.is_symlink() and path.is_file()
    }
    return sorted(
        path for path in paths if path.suffix != ".zst" or path.with_suffix("") not in paths
    )


def session_blocks(paths: list[Path], block_size: int) -> Iterator[bytes]:
    for path in paths:
        with zstd.open(path, "rb") if path.suffix == ".zst" else path.open("rb") as source:
            while block := source.read(block_size):
                yield block


def benchmark(paths: list[Path], block_mib: int) -> tuple[int, dict[int, Measurement]]:
    block_size = block_mib * MIB
    results = {level: Measurement() for level in LEVELS}
    raw_bytes = 0
    for block in session_blocks(paths, block_size):
        raw_bytes += len(block)
        for level, result in results.items():
            result.add(block, block_size, level)
    return raw_bytes, results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, help="Directory of local session files")
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error("--root must be a readable directory containing session files.")

    paths = session_files(args.root)
    if not paths:
        parser.error("--root must contain .jsonl or .jsonl.zst session files.")

    print("block_MiB level stored_pct compress_MiB/s decompress_MiB/s median_full_block_ms")
    selected_level = 3
    corpus_bytes = 0
    for block_mib in BLOCK_SIZES_MIB:
        try:
            raw_bytes, results = benchmark(paths, block_mib)
        except (OSError, EOFError, zstd.ZstdError) as error:
            parser.error(f"Could not read the session corpus ({type(error).__name__}).")
        if not raw_bytes:
            parser.error("The session corpus is empty.")
        if corpus_bytes and corpus_bytes != raw_bytes:
            parser.error("Session sizes changed during the run. Use a stable session corpus.")
        corpus_bytes = raw_bytes
        raw_mib = raw_bytes / MIB

        for level, result in results.items():
            samples = result.full_block_ms
            median_ms = statistics.median(samples) if samples else None
            median_text = f"{median_ms:.2f}" if median_ms is not None else "n/a"
            print(
                f"{block_mib:9} {level:5} {100 * result.stored_bytes / raw_bytes:10.2f} "
                f"{raw_mib / result.compress_seconds:14.2f} "
                f"{raw_mib / result.decompress_seconds:16.2f} {median_text:>20}",
                flush=True,
            )
            if block_mib == 4 and median_ms is not None and median_ms <= 150:
                selected_level = level
        if block_mib == 4:
            full_blocks = len(results[3].full_block_ms)
            print(f"selected_level={selected_level} full_4_MiB_blocks={full_blocks}", flush=True)
    print(f"input_files={len(paths)} input_bytes={corpus_bytes}")


if __name__ == "__main__":
    main()
