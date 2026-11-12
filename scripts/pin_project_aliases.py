"""Persist the current aliases of projects registered before WD-216."""

import argparse
from pathlib import Path

from agent_watchdog.config import UserPaths, load_config, save_config
from agent_watchdog.daemon import control_lock
from agent_watchdog.registry import Registry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--runtime", required=True, type=Path)
    args = parser.parse_args()
    paths = UserPaths(args.config, args.data, args.runtime)
    with control_lock(paths):
        config = load_config(paths.config)
        missing = sum(project.alias is None for project in config.projects)
        if missing:
            save_config(paths.config, Registry(config).config)
    print(f"Saved {missing} project aliases.")


if __name__ == "__main__":
    main()
