"""Helpers shared by tests that run real scans: local HTTP servers and a controllable clock."""

from __future__ import annotations

import importlib.util
import itertools
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from evidence_room.models import Client, Matter, Touchpoint

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
EXAMPLES = FIXTURES / "scanner-examples"
T0 = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
_numbers = itertools.count(1)


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        return self.now


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@contextmanager
def serve(directory: Path) -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(directory)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def point(link: Path, site: str) -> None:
    """Point a symlinked site folder at northwind-<site>."""
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(EXAMPLES / "sites" / f"northwind-{site}", target_is_directory=True)


def load_sink() -> ModuleType:
    path = ROOT / "docker" / "webhook-sink" / "sink.py"
    spec = importlib.util.spec_from_file_location("webhook_sink", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_touchpoint(
    sf: sessionmaker[Session],
    kind: str,
    target: str,
    *,
    schedule: str = "manual",
    next_run_at: datetime | None = T0,
    options: dict[str, Any] | None = None,
) -> int:
    n = next(_numbers)
    with sf() as s, s.begin():
        client = Client(name=f"Northwind test client {n}")
        s.add(client)
        s.flush()
        matter = Matter(client_id=client.id, title=f"Art. 50 review {n}")
        s.add(matter)
        s.flush()
        tp = Touchpoint(
            matter_id=matter.id,
            kind=kind,
            target=target,
            party_role="deployer",
            schedule=schedule,
            next_run_at=next_run_at,
            options=options or {},
        )
        s.add(tp)
        s.flush()
        return tp.id
