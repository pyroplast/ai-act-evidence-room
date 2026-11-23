"""The worker: a plain loop over ``next_run_at``. No scheduler library (D-018).

Each ``tick``:

1. **Reap** scans left ``running`` longer than SCAN_STALE_MINUTES (their worker died):
   mark them ``error``, audit ``scan.failed`` and queue the event.
2. **Claim** due touchpoints, in one short transaction:

       SELECT ... FROM touchpoints
       WHERE active AND next_run_at <= now
         AND no scan of it is running
       ORDER BY next_run_at FOR UPDATE SKIP LOCKED LIMIT n

   and move each one's ``next_run_at`` forward (daily +1 day, weekly +7 days, manual
   NULL) before committing. Once committed, the row is no longer due, so no other
   worker can pick it up. ``SKIP LOCKED`` lets a second worker claim *other* rows
   instead of waiting. Two workers therefore never run the same touchpoint.
3. **Run** each claimed scan with ``runner.run_scan`` (which queues its events), then
   email the digest if email is configured.
4. **Deliver** due webhook attempts (``notify.deliver_due``).
"""

from __future__ import annotations

import logging
import os
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ai_disclosure_scan import api as scanner_api
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from evidence_room import audit, notify
from evidence_room.config import Settings, get_settings
from evidence_room.db import session_factory as default_session_factory
from evidence_room.runner import run_scan
from evidence_room.store import ArtifactStore

log = logging.getLogger("evidence_room.worker")

ACTOR = "system:worker"
INTERVALS = {"daily": timedelta(days=1), "weekly": timedelta(days=7)}

CLAIM_SQL = text(
    """
    SELECT t.id, t.schedule
    FROM touchpoints t
    WHERE t.active
      AND t.next_run_at IS NOT NULL
      AND t.next_run_at <= :now
      AND NOT EXISTS (
          SELECT 1 FROM scans s
          WHERE s.touchpoint_id = t.id AND s.status IN ('queued', 'running')
      )
    ORDER BY t.next_run_at, t.id
    FOR UPDATE OF t SKIP LOCKED
    LIMIT :limit
    """
)

REAP_SQL = text(
    """
    UPDATE scans
       SET status = 'error', error = :error, finished_at = :now
     WHERE status IN ('queued', 'running') AND started_at < :cutoff
    RETURNING id, touchpoint_id
    """
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def next_run(schedule: str, now: datetime) -> datetime | None:
    interval = INTERVALS.get(schedule)
    return now + interval if interval else None


@dataclass
class TickResult:
    reaped: list[int] = field(default_factory=list)
    scans: list[int] = field(default_factory=list)
    deliveries: int = 0

    @property
    def busy(self) -> bool:
        return bool(self.reaped or self.scans or self.deliveries)


@dataclass
class Worker:
    session_factory: sessionmaker[Session]
    store: ArtifactStore
    settings: Settings
    clock: Callable[[], datetime] = utcnow
    scanner: Any = scanner_api
    send: notify.Sender = notify.http_send
    smtp_factory: Any = None  # defaults to smtplib.SMTP inside notify.send_digest
    batch: int = 5
    name: str = "worker"

    # --- the steps ---------------------------------------------------------------------

    def reap_stale(self) -> list[int]:
        now = self.clock()
        cutoff = now - timedelta(minutes=self.settings.scan_stale_minutes)
        message = (
            f"abandoned: still running after {self.settings.scan_stale_minutes} minutes; "
            "the worker running it stopped"
        )
        with self.session_factory() as s, s.begin():
            rows = s.execute(REAP_SQL, {"error": message, "now": now, "cutoff": cutoff}).all()
            for scan_id, touchpoint_id in rows:
                audit.append(
                    s,
                    ACTOR,
                    "scan.failed",
                    "scan",
                    scan_id,
                    {"touchpoint_id": touchpoint_id, "error": message, "artifacts": []},
                    clock=self.clock,
                )
                notify.queue_events(
                    s, scan_id, clock=self.clock, base_url=self.settings.app_base_url
                )
        return [r[0] for r in rows]

    def claim(self, limit: int | None = None) -> list[int]:
        now = self.clock()
        with self.session_factory() as s, s.begin():
            rows = s.execute(CLAIM_SQL, {"now": now, "limit": limit or self.batch}).all()
            for touchpoint_id, schedule in rows:
                s.execute(
                    text("UPDATE touchpoints SET next_run_at = :n WHERE id = :id"),
                    {"n": next_run(schedule, now), "id": touchpoint_id},
                )
        return [r[0] for r in rows]

    def scan(self, touchpoint_id: int) -> int:
        scan_id = run_scan(
            self.session_factory,
            touchpoint_id,
            store=self.store,
            triggered_by=f"schedule:{self.name}",
            clock=self.clock,
            scanner=self.scanner,
            base_url=self.settings.app_base_url,
        )
        if self.settings.email_enabled:
            self._email(scan_id)
        return scan_id

    def _email(self, scan_id: int) -> None:
        with self.session_factory() as s:
            events = notify.events_for_scan(
                s, scan_id, base_url=self.settings.app_base_url, occurred_at=self.clock()
            )
        if not events:
            return
        kwargs = {"smtp_factory": self.smtp_factory} if self.smtp_factory else {}
        try:
            notify.send_digest(self.settings, scan_id, events, **kwargs)
            log.info("emailed digest for scan %s (%d events)", scan_id, len(events))
        except Exception as exc:  # email is best effort; webhooks are the record
            log.warning("email digest for scan %s failed: %s", scan_id, exc)

    def tick(self) -> TickResult:
        result = TickResult(reaped=self.reap_stale())
        while True:
            claimed = self.claim()
            if not claimed:
                break
            for touchpoint_id in claimed:
                result.scans.append(self.scan(touchpoint_id))
        result.deliveries = notify.deliver_due(
            self.session_factory, clock=self.clock, send=self.send
        )
        return result


class _Stop:
    requested = False


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = get_settings()
    stop = _Stop()

    def _request_stop(signum: int, _frame: object) -> None:
        log.info("signal %s received, stopping after the current tick", signum)
        stop.requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    worker = Worker(
        session_factory=default_session_factory(),
        store=ArtifactStore(settings.artifact_store),
        settings=settings,
        name=f"worker-{os.getpid()}",
    )
    log.info(
        "worker started: poll %.1fs, email %s",
        settings.worker_poll_seconds,
        "on" if settings.email_enabled else "off",
    )
    while not stop.requested:
        try:
            result = worker.tick()
            if result.busy:
                log.info(
                    "tick: reaped %s, scanned %s, delivery attempts %d",
                    result.reaped,
                    result.scans,
                    result.deliveries,
                )
        except Exception:
            log.exception("tick failed; retrying after the poll interval")
        if not stop.requested:
            time.sleep(settings.worker_poll_seconds)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
