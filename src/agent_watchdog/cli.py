import argparse


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-watchdog",
        description="Watchdog foundation; collection is not implemented yet. See ROADMAP.md.",
    )
    parser.parse_args()
    parser.print_help()
    return 0
