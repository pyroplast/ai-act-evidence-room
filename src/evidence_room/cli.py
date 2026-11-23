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

    hook = sub.add_parser("webhook", help="manage webhook subscriptions")
    hook_sub = hook.add_subparsers(dest="webhook_command", required=True)
    add = hook_sub.add_parser("add", help="subscribe a URL to events")
    add.add_argument("--url", required=True)
    add.add_argument(
        "--secret",
        help="shared HMAC secret (default: generate one and print it once)",
    )
    add.add_argument(
        "--event",
        action="append",
        dest="events",
        choices=["finding.regressed", "finding.resolved", "scan.failed"],
        help="repeat to subscribe to several; default: all events",
    )
    hook_sub.add_parser("list", help="list webhooks and their delivery counts")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "worker":
        from evidence_room.worker import main as worker_main

        worker_main()
        return 0
    if args.command == "scan":
        return _scan(args.touchpoint, args.triggered_by)
    if args.command == "webhook":
        return _webhook(args)
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


def _webhook(args: argparse.Namespace) -> int:
    import secrets

    from sqlalchemy import func, select

    from evidence_room import audit
    from evidence_room.db import session_factory
    from evidence_room.models import Delivery, Webhook

    sf = session_factory()
    if args.webhook_command == "add":
        secret = args.secret or secrets.token_hex(32)
        with sf() as s, s.begin():
            hook = Webhook(url=args.url, secret=secret, events=sorted(set(args.events or [])))
            s.add(hook)
            s.flush()
            # The secret is never written to the audit log.
            audit.append(
                s,
                "cli",
                "webhook.created",
                "webhook",
                hook.id,
                {"url": hook.url, "events": hook.events},
            )
            print(f"webhook {hook.id}: {hook.url} events={hook.events or 'all'}")
        if not args.secret:
            print(f"secret (shown once): {secret}")
        return 0
    with sf() as s:
        counts = dict(
            s.execute(select(Delivery.webhook_id, func.count()).group_by(Delivery.webhook_id)).all()
        )
        for hook in s.scalars(select(Webhook).order_by(Webhook.id)):
            state = "active" if hook.active else "disabled"
            print(
                f"{hook.id}  {state:<8} {hook.url}  events={hook.events or 'all'}  "
                f"attempts={counts.get(hook.id, 0)}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
