"""``evidence-room`` command line. Subcommands are added milestone by milestone."""

from __future__ import annotations

import argparse
import sys

from evidence_room import SCOPE_LINE, __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evidence-room", description=SCOPE_LINE)
    parser.add_argument("--version", action="version", version=f"evidence-room {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("worker", help="run the scheduler loop")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "worker":
        from evidence_room.worker import main as worker_main

        worker_main()
        return 0
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
