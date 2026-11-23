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
    scan = sub.add_parser("scan", help="scan one touchpoint now and print the outcome")
    scan.add_argument("--touchpoint", type=int, required=True, metavar="ID")
    scan.add_argument("--triggered-by", default="cli", metavar="WHO")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "worker":
        from evidence_room.worker import main as worker_main

        worker_main()
        return 0
    if args.command == "scan":
        return _scan(args.touchpoint, args.triggered_by)
    build_parser().print_help()
    return 0


def _scan(touchpoint_id: int, triggered_by: str) -> int:
    from evidence_room.config import get_settings
    from evidence_room.db import session_factory
    from evidence_room.models import Finding, Scan
    from evidence_room.runner import run_scan
    from evidence_room.store import ArtifactStore

    sf = session_factory()
    scan_id = run_scan(
        sf,
        touchpoint_id,
        store=ArtifactStore(get_settings().artifact_store),
        triggered_by=triggered_by,
    )
    with sf() as session:
        scan = session.get(Scan, scan_id)
        assert scan is not None
        if scan.status != "done":
            print(f"scan {scan_id}: {scan.status}: {scan.error}", file=sys.stderr)
            return 1
        rows = session.query(Finding).filter(Finding.scan_id == scan_id).order_by(Finding.id)
        print(f"scan {scan_id}: done")
        for f in rows:
            print(f"  {f.rule_id:<9} {f.status:<7} Art. {f.paragraph:<14} {f.target}")
    print(SCOPE_LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
