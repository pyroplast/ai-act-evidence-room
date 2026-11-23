"""Worker process. M0: a heartbeat loop that proves the container can reach the database.

M3 replaces the body of ``tick`` with: claim due touchpoints using
``SELECT ... FOR UPDATE SKIP LOCKED``, run each scan, set ``next_run_at``.
"""

from __future__ import annotations

import logging
import signal
import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from evidence_room.config import get_settings
from evidence_room.db import get_engine

log = logging.getLogger("evidence_room.worker")


class _Stop:
    requested = False


def tick() -> None:
    with get_engine().connect() as conn:
        conn.execute(text("SELECT 1"))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    stop = _Stop()

    def _request_stop(signum: int, _frame: object) -> None:
        log.info("signal %s received, stopping after current tick", signum)
        stop.requested = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    interval = get_settings().worker_poll_seconds
    log.info("worker started, poll interval %.1fs", interval)
    while not stop.requested:
        try:
            tick()
        except SQLAlchemyError as exc:
            log.warning("database not reachable: %s", type(exc).__name__)
        time.sleep(interval)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
