"""M3 acceptance: scheduler, regressions, signed webhooks with retries, email digest.

Everything runs against 127.0.0.1: the Northwind fixtures and the demo webhook sink
(docker/webhook-sink/sink.py, loaded in-process) are served by threads in this process.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest
from ai_disclosure_scan import api as scanner_api
from sqlalchemy import Engine, create_engine, func, select, text
from sqlalchemy.orm import Session, sessionmaker

from evidence_room import SCOPE_LINE, audit, notify
from evidence_room.config import Settings
from evidence_room.models import Artifact, AuditLog, Delivery, Finding, Scan, Touchpoint, Webhook
from evidence_room.runner import run_scan
from evidence_room.store import ArtifactStore
from evidence_room.worker import CLAIM_SQL, Worker
from tests.support import EXAMPLES, T0, Clock, load_sink, make_touchpoint, point, serve

SECRET = "test-webhook-secret"


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "database_url": "unused",
        "artifact_store": "unused",
        "worker_poll_seconds": 0.0,
        "app_base_url": "http://evidence.test",
        "scan_stale_minutes": 60,
        "smtp_host": "",
        "smtp_port": 587,
        "smtp_user": "",
        "smtp_password": "",
        "smtp_starttls": True,
        "digest_from": "evidence-room@firm.test",
        "digest_to": (),
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def sf(fresh_db_url: str) -> Iterator[sessionmaker[Session]]:
    engine: Engine = create_engine(fresh_db_url, pool_size=10)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "store")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def worker(sf: sessionmaker[Session], store: ArtifactStore, clock: Clock) -> Worker:
    return Worker(sf, store, settings(), clock=clock)


@pytest.fixture
def sink(tmp_path: Path) -> Iterator[tuple[Any, str]]:
    module = load_sink()
    # Tolerance 0: the worker signs with the test's frozen clock, not wall time.
    state = module.Sink(SECRET, tmp_path / "sink.jsonl", tolerance=0)
    server = module.make_server(state, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_address[1]}/hooks/evidence"
    server.shutdown()
    server.server_close()


@pytest.fixture
def site(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    root = tmp_path / "www"
    root.mkdir()
    link = root / "northwind"
    point(link, "good")
    with serve(root) as base:
        yield f"{base}/northwind/", link


def add_webhook(
    sf: sessionmaker[Session], url: str, secret: str = SECRET, events: list[str] | None = None
) -> int:
    with sf() as s, s.begin():
        hook = Webhook(url=url, secret=secret, events=events or [])
        s.add(hook)
        s.flush()
        return hook.id


def deliveries(sf: sessionmaker[Session]) -> list[Delivery]:
    with sf() as s:
        return list(s.scalars(select(Delivery).order_by(Delivery.id)))


# --- scheduling ---------------------------------------------------------------------------


def test_skip_locked_hands_a_second_claimer_other_rows(sf: sessionmaker[Session]) -> None:
    for _ in range(4):
        make_touchpoint(sf, "media", str(EXAMPLES / "media"))
    with sf() as a, sf() as b:
        a.begin()
        b.begin()
        first = [r[0] for r in a.execute(CLAIM_SQL, {"now": T0, "limit": 2})]
        second = [r[0] for r in b.execute(CLAIM_SQL, {"now": T0, "limit": 10})]
        assert len(first) == 2
        assert len(second) == 2
        assert not set(first) & set(second)  # locked rows are skipped, not waited for
        a.rollback()
        b.rollback()


class SlowScanner:
    """The real scanner, slowed down so two workers really overlap."""

    def __getattr__(self, name: str) -> Any:
        return getattr(scanner_api, name)

    def scan_media(self, paths: Any, **kw: Any) -> Any:
        threading.Event().wait(0.05)
        return scanner_api.scan_media(paths, **kw)


def test_two_workers_never_run_the_same_touchpoint(
    sf: sessionmaker[Session], store: ArtifactStore, clock: Clock
) -> None:
    ids = [
        make_touchpoint(sf, "media", str(EXAMPLES / "media"), schedule="daily") for _ in range(10)
    ]
    workers = [
        Worker(sf, store, settings(), clock=clock, scanner=SlowScanner(), batch=1, name=n)
        for n in ("w1", "w2")
    ]
    results: dict[str, list[int]] = {}
    start = threading.Barrier(2)

    def run(w: Worker) -> None:
        start.wait()
        results[w.name] = w.tick().scans

    threads = [threading.Thread(target=run, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with sf() as s:
        per_touchpoint = dict(
            s.execute(select(Scan.touchpoint_id, func.count()).group_by(Scan.touchpoint_id)).all()
        )
        assert per_touchpoint == dict.fromkeys(ids, 1)  # every touchpoint exactly once
        assert len(results["w1"]) + len(results["w2"]) == 10
        next_runs = set(s.scalars(select(Touchpoint.next_run_at)))
        assert next_runs == {T0 + timedelta(days=1)}
        triggered = {row for row in s.scalars(select(Scan.triggered_by))}
        assert triggered <= {"schedule:w1", "schedule:w2"}


def test_schedules_move_next_run_at(
    sf: sessionmaker[Session], worker: Worker, clock: Clock
) -> None:
    daily = make_touchpoint(sf, "media", str(EXAMPLES / "media"), schedule="daily")
    weekly = make_touchpoint(sf, "media", str(EXAMPLES / "media"), schedule="weekly")
    manual = make_touchpoint(sf, "media", str(EXAMPLES / "media"), schedule="manual")
    later = make_touchpoint(
        sf, "media", str(EXAMPLES / "media"), next_run_at=T0 + timedelta(hours=1)
    )
    assert sorted(worker.claim(limit=10)) == sorted([daily, weekly, manual])
    with sf() as s:
        nxt = {tp.id: tp.next_run_at for tp in s.scalars(select(Touchpoint))}
    assert nxt[daily] == T0 + timedelta(days=1)
    assert nxt[weekly] == T0 + timedelta(days=7)
    assert nxt[manual] is None
    assert nxt[later] == T0 + timedelta(hours=1)
    assert worker.claim(limit=10) == []  # nothing is due twice


# --- regressions and webhooks ---------------------------------------------------------------


def test_good_to_bad_emits_one_regressed_event_per_new_finding(
    sf: sessionmaker[Session],
    worker: Worker,
    clock: Clock,
    sink: tuple[Any, str],
    site: tuple[str, Path],
) -> None:
    sink_state, sink_url = sink
    url, link = site
    add_webhook(sf, sink_url)
    tp = make_touchpoint(sf, "page", url, schedule="daily")

    first = worker.tick()
    assert len(first.scans) == 1
    assert deliveries(sf) == []  # the first scan is a baseline: nothing to report

    point(link, "bad")
    clock.advance(days=1)
    second = worker.tick()
    assert len(second.scans) == 1
    bad_scan = second.scans[0]

    sent = deliveries(sf)
    assert sorted((d.event, d.payload["finding"]["rule_id"]) for d in sent) == [
        ("finding.regressed", "LBL-001"),
        ("finding.regressed", "PAGE-001"),
    ]
    assert all(d.attempt == 1 and d.status_code == 200 and d.delivered_at for d in sent)

    received = sink_state.received()
    assert len(received) == 2
    assert all(r["verified"] and r["responded"] == 200 for r in received)

    with sf() as s:
        html = s.scalars(
            select(Artifact).where(Artifact.scan_id == bad_scan, Artifact.kind == "html")
        ).one()
        page001 = s.scalars(
            select(Finding).where(Finding.scan_id == bad_scan, Finding.rule_id == "PAGE-001")
        ).one()
        emitted = s.scalars(select(AuditLog).where(AuditLog.action == "event.emitted")).all()
        assert sorted(e.payload["rule_id"] for e in emitted) == ["LBL-001", "PAGE-001"]
        assert audit.verify_chain(s).ok

    p = next(r["payload"] for r in received if r["payload"]["finding"]["rule_id"] == "PAGE-001")
    assert p["event"] == "finding.regressed"
    assert p["matter"]["client"].startswith("Northwind test client")
    assert p["touchpoint"] == {"id": tp, "kind": "page", "target": url}
    assert p["finding"]["paragraph"] == "50(1), 50(5)"
    assert (p["finding"]["previous_status"], p["finding"]["status"]) == ("absent", "review")
    assert p["finding"]["basis"] and p["finding"]["basis"][0]["url"].startswith("https://")
    assert p["evidence"]["sha256"] == html.sha256
    assert p["link"] == f"http://evidence.test/findings/{page001.id}"
    assert p["scope"] == SCOPE_LINE


def test_resolution_is_reported_when_the_page_is_fixed(
    sf: sessionmaker[Session],
    worker: Worker,
    clock: Clock,
    sink: tuple[Any, str],
    site: tuple[str, Path],
) -> None:
    _, sink_url = sink
    url, link = site
    point(link, "bad")
    add_webhook(sf, sink_url)
    make_touchpoint(sf, "page", url, schedule="daily")
    worker.tick()
    point(link, "good")
    clock.advance(days=1)
    worker.tick()
    events = sorted((d.event, d.payload["finding"]["rule_id"]) for d in deliveries(sf))
    assert events == [("finding.resolved", "LBL-001"), ("finding.resolved", "PAGE-001")]


def test_webhook_event_filter(
    sf: sessionmaker[Session],
    worker: Worker,
    clock: Clock,
    sink: tuple[Any, str],
    site: tuple[str, Path],
) -> None:
    _, sink_url = sink
    url, link = site
    add_webhook(sf, sink_url, events=["scan.failed"])
    make_touchpoint(sf, "page", url, schedule="daily")
    worker.tick()
    point(link, "bad")
    clock.advance(days=1)
    worker.tick()
    assert deliveries(sf) == []  # regressions happened, but this hook wants failures only


def test_sink_rejects_a_bad_signature(
    sf: sessionmaker[Session], worker: Worker, sink: tuple[Any, str]
) -> None:
    sink_state, sink_url = sink
    add_webhook(sf, sink_url, secret="not-the-sinks-secret")
    make_touchpoint(sf, "media", "/no/such/folder")  # fails -> scan.failed
    worker.tick()
    (attempt, retry) = deliveries(sf)
    assert (attempt.status_code, attempt.delivered_at) == (401, None)
    assert retry.attempt == 2 and retry.attempted_at is None
    (got,) = sink_state.received()
    assert (got["verified"], got["reason"]) == (False, "signature mismatch")


def test_failed_scan_event_and_retries_on_schedule(
    sf: sessionmaker[Session], worker: Worker, clock: Clock, sink: tuple[Any, str]
) -> None:
    sink_state, sink_url = sink
    sink_state.fail_status = 500
    add_webhook(sf, sink_url)
    make_touchpoint(sf, "media", "/no/such/folder")

    start = clock()
    assert worker.tick().deliveries == 1  # the scan fails; scan.failed goes out at once

    expected_gaps = [1, 5, 30, 120]  # minutes after each failed attempt
    attempt_times = [start]
    for gap in expected_gaps:
        due = attempt_times[-1] + timedelta(minutes=gap)
        clock.now = due - timedelta(seconds=1)
        assert worker.tick().deliveries == 0, f"attempt before its {gap}-minute delay"
        clock.now = due
        assert worker.tick().deliveries == 1
        attempt_times.append(due)

    clock.advance(days=1)
    assert worker.tick().deliveries == 0  # given up after the fifth attempt

    rows = deliveries(sf)
    assert [d.attempt for d in rows] == [1, 2, 3, 4, 5]
    assert [d.attempted_at for d in rows] == attempt_times
    assert all(d.status_code == 500 and d.delivered_at is None for d in rows)
    assert {d.event for d in rows} == {"scan.failed"}
    assert len({d.event_id for d in rows}) == 1
    received = sink_state.received()
    assert len(received) == 5
    assert all(r["verified"] for r in received)
    assert [r["attempt"] for r in received] == ["1", "2", "3", "4", "5"]
    payload = rows[0].payload
    assert "no such file or folder" in payload["scan"]["error"]
    assert payload["link"].startswith("http://evidence.test/scans/")


def test_delivery_succeeds_once_the_receiver_recovers(
    sf: sessionmaker[Session], worker: Worker, clock: Clock, sink: tuple[Any, str]
) -> None:
    sink_state, sink_url = sink
    sink_state.fail_status = 503
    add_webhook(sf, sink_url)
    make_touchpoint(sf, "media", "/no/such/folder")
    worker.tick()
    sink_state.fail_status = 0
    clock.advance(minutes=1)
    worker.tick()
    clock.advance(days=1)
    worker.tick()
    rows = deliveries(sf)
    assert [(d.attempt, d.status_code, d.delivered_at is not None) for d in rows] == [
        (1, 503, False),
        (2, 200, True),
    ]


def test_unreachable_receiver_is_retried(sf: sessionmaker[Session], worker: Worker) -> None:
    add_webhook(sf, "http://127.0.0.1:9/nothing-listens-here")
    make_touchpoint(sf, "media", "/no/such/folder")
    worker.tick()
    first, retry = deliveries(sf)
    assert first.status_code is None and first.error
    assert retry.attempt == 2


# --- failure handling ----------------------------------------------------------------------


def test_stale_running_scan_is_reaped_and_reported(
    sf: sessionmaker[Session], worker: Worker, clock: Clock, sink: tuple[Any, str]
) -> None:
    _, sink_url = sink
    add_webhook(sf, sink_url)
    tp = make_touchpoint(sf, "media", str(EXAMPLES / "media"), next_run_at=None)
    with sf() as s, s.begin():
        s.add(
            Scan(
                touchpoint_id=tp,
                status="running",
                triggered_by="crashed-worker",
                started_at=clock() - timedelta(minutes=61),
            )
        )
    with sf() as s, s.begin():  # "scan now" while the dead scan still looks busy
        s.execute(text("UPDATE touchpoints SET next_run_at = :n"), {"n": clock()})

    result = worker.tick()
    assert len(result.reaped) == 1
    assert len(result.scans) == 1  # the touchpoint is free again and runs right away
    with sf() as s:
        reaped = s.get(Scan, result.reaped[0])
        assert reaped is not None and reaped.status == "error"
        assert reaped.error is not None and reaped.error.startswith("abandoned")
    (sent,) = deliveries(sf)
    assert sent.event == "scan.failed" and sent.delivered_at is not None


def test_runner_does_not_overwrite_a_reaped_scan(
    sf: sessionmaker[Session], store: ArtifactStore, clock: Clock
) -> None:
    tp = make_touchpoint(sf, "media", str(EXAMPLES / "media"))

    class ReapedMidScan:
        def __getattr__(self, name: str) -> Any:
            return getattr(scanner_api, name)

        def scan_media(self, paths: Any, **kw: Any) -> Any:
            with sf() as s, s.begin():
                s.execute(
                    text(
                        "UPDATE scans SET status='error', error='abandoned (test)', "
                        "finished_at=:n"
                    ),
                    {"n": clock()},
                )
            return scanner_api.scan_media(paths, **kw)

    scan_id = run_scan(sf, tp, store=store, clock=clock, scanner=ReapedMidScan())
    with sf() as s:
        scan = s.get(Scan, scan_id)
        assert scan is not None and (scan.status, scan.error) == ("error", "abandoned (test)")
        assert s.scalar(select(func.count()).select_from(Finding)) == 0


# --- email digest ----------------------------------------------------------------------------


class FakeSMTP:
    sent: ClassVar[list[EmailMessage]] = []
    calls: ClassVar[list[str]] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.calls.append(f"connect {host}:{port}")

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        pass

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login {user}")

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


def test_email_digest_when_configured(
    sf: sessionmaker[Session], store: ArtifactStore, clock: Clock, site: tuple[str, Path]
) -> None:
    FakeSMTP.sent, FakeSMTP.calls = [], []
    cfg = settings(smtp_host="smtp.firm.test", smtp_user="robot", digest_to=("lawyer@firm.test",))
    worker = Worker(sf, store, cfg, clock=clock, smtp_factory=FakeSMTP)
    url, link = site
    make_touchpoint(sf, "page", url, schedule="daily")
    worker.tick()
    assert FakeSMTP.sent == []  # baseline scan: no events, no email
    point(link, "bad")
    clock.advance(days=1)
    worker.tick()

    (msg,) = FakeSMTP.sent
    assert FakeSMTP.calls == ["connect smtp.firm.test:587", "starttls", "login robot"]
    assert msg["To"] == "lawyer@firm.test"
    assert "2 change(s)" in msg["Subject"]
    body = msg.get_content()
    assert "REGRESSED  PAGE-001  Art. 50(1), 50(5)  absent -> review" in body
    assert "REGRESSED  LBL-001" in body
    assert "evidence sha256 " in body
    assert body.rstrip().endswith(SCOPE_LINE)


def test_email_is_off_by_default(
    sf: sessionmaker[Session], worker: Worker, clock: Clock, site: tuple[str, Path]
) -> None:
    FakeSMTP.sent = []
    worker.smtp_factory = FakeSMTP
    url, link = site
    make_touchpoint(sf, "page", url, schedule="daily")
    worker.tick()
    point(link, "bad")
    clock.advance(days=1)
    worker.tick()
    assert FakeSMTP.sent == []


# --- signing -----------------------------------------------------------------------------------


def test_signature_roundtrip_and_sink_agrees() -> None:
    sink_module = load_sink()
    body = notify.canonical_body({"b": 1, "a": "ü"})
    assert body == '{"a":"ü","b":1}'.encode()
    header = notify.sign(SECRET, 1_785_000_000, body)
    assert header.startswith("t=1785000000,v1=")
    for check in (notify.verify, sink_module.verify):
        assert check(SECRET, header, body, now=1_785_000_100) == (True, "ok")
        assert check(SECRET, header, body + b" ", now=1_785_000_100)[1] == "signature mismatch"
        assert check("other", header, body, now=1_785_000_100)[1] == "signature mismatch"
        assert check(SECRET, header, body, now=1_785_001_000)[1] == "timestamp outside tolerance"
        assert check(SECRET, "garbage", body)[1] == "malformed signature header"
