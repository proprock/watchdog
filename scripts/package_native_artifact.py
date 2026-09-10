"""Package one locked Rust adapter binary into its portable release archive."""

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path

from agent_watchdog.native_artifacts import PACKAGE, archive_name, executable_for_target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", required=True, type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        executable = executable_for_target(args.target)
    except ValueError as error:
        parser.error(str(error))
    if not args.binary.is_file() or args.binary.name != executable:
        parser.error(f"--binary must name the built {executable} executable")
    if not args.version or any(character.isspace() for character in args.version):
        parser.error("--version must be a nonempty version without whitespace")

    payload = args.binary.read_bytes()
    manifest = {
        "schema_version": 1,
        "package": PACKAGE,
        "version": args.version,
        "target": args.target,
        "executable": executable,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    archive = args.output_dir / archive_name(args.version, args.target)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(executable, payload)
        output.writestr("manifest.json", json.dumps(manifest, sort_keys=True) + "\n")
    print(archive)
    return 0


if __name__ == "__main__":
    sys.exit(main())
