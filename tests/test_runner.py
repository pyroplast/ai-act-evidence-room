"""M2 acceptance: the runner on top of ai-disclosure-scan, against the local Northwind fixtures.

Pages are served from 127.0.0.1 by a thread in this process; nothing touches the
public internet. Each test gets its own database because the runner commits.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from ai_disclosure_scan import api as scanner_api
from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from evidence_room import audit
from evidence_room.models import Artifact, AuditLog, Client, Finding, Matter, Scan, Touchpoint
from evidence_room.runner import run_scan
from evidence_room.store import ArtifactStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLES = FIXTURES / "scanner-examples"
T0 = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)
_client_numbers = itertools.count(1)


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


@pytest.fixture(scope="module")
def site() -> Iterator[str]:
    handler = partial(_Quiet, directory=str(EXAMPLES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def db(fresh_db_url: str) -> Iterator[Engine]:
    engine = create_engine(fresh_db_url)
    yield engine
    engine.dispose()


@pytest.fixture
def sf(db: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db, expire_on_commit=False)


@pytest.fixture
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "store")


def ticking(start: datetime = T0) -> Any:
    n = iter(range(100_000))
    return lambda: start + timedelta(seconds=next(n))


def frozen(at: datetime = T0) -> Any:
    return lambda: at


def touchpoint(
    sf: sessionmaker[Session], kind: str, target: str, options: dict[str, Any] | None = None
) -> int:
    with sf() as s, s.begin():
        client = Client(name=f"Northwind client {next(_client_numbers)}")
        s.add(client)
        s.flush()
        matter = Matter(client_id=client.id, title="Art. 50 transparency review")
        s.add(matter)
        s.flush()
        tp = Touchpoint(
            matter_id=matter.id,
            kind=kind,
            target=target,
            party_role="deployer",
            options=options or {},
        )
        s.add(tp)
        s.flush()
        return tp.id


def scan_row(sf: sessionmaker[Session], scan_id: int) -> Scan:
    with sf() as s:
        row = s.get(Scan, scan_id)
        assert row is not None
        return row


def findings(sf: sessionmaker[Session], scan_id: int) -> list[Finding]:
    with sf() as s:
        return list(
            s.scalars(select(Finding).where(Finding.scan_id == scan_id).order_by(Finding.id))
        )


def artifacts(sf: sessionmaker[Session], scan_id: int) -> list[Artifact]:
    with sf() as s:
        return list(
            s.scalars(select(Artifact).where(Artifact.scan_id == scan_id).order_by(Artifact.id))
        )


def statuses(rows: list[Finding]) -> set[tuple[str, str]]:
    return {(f.rule_id, f.status) for f in rows}


# --- pages ---------------------------------------------------------------------------


def test_northwind_bad_page_flags_the_widget_and_the_unlabelled_media(
    sf: sessionmaker[Session], store: ArtifactStore, site: str
) -> None:
    """Static scan of northwind-bad. The scanner rates both signals REVIEW, not GAP: from
    the page HTML alone it cannot see inside the widget (docs/decisions.md, D-012)."""
    tp = touchpoint(sf, "page", f"{site}/sites/northwind-bad/")
    scan_id = run_scan(sf, tp, store=store, clock=ticking())

    scan = scan_row(sf, scan_id)
    assert (scan.status, scan.error) == ("done", None)
    assert scan.scanner_version == scanner_api.scanner_version()
    rows = findings(sf, scan_id)
    assert statuses(rows) == {("PAGE-001", "review"), ("LBL-001", "review")}
    assert not any(f.status == "pass" for f in rows)


def test_northwind_good_page_passes(
    sf: sessionmaker[Session], store: ArtifactStore, site: str
) -> None:
    tp = touchpoint(sf, "page", f"{site}/sites/northwind-good/")
    scan_id = run_scan(sf, tp, store=store, clock=ticking())
    assert scan_row(sf, scan_id).status == "done"
    assert statuses(findings(sf, scan_id)) == {("PAGE-003", "pass"), ("LBL-002", "pass")}


def test_page_artifacts_are_stored_by_sha256_and_audited(
    sf: sessionmaker[Session], store: ArtifactStore, site: str
) -> None:
    tp = touchpoint(sf, "page", f"{site}/sites/northwind-bad/")
    scan_id = run_scan(sf, tp, store=store, clock=ticking())

    rows = artifacts(sf, scan_id)
    by_kind = sorted((a.kind, Path(a.name).name) for a in rows)
    assert by_kind == [
        ("html", "northwind-bad"),  # the page URL, as fetched
        ("media", "hero.png"),
        ("media", "team.png"),
        ("report_json", "report.json"),
    ]
    html = next(a for a in rows if a.kind == "html")
    assert store.get(html.sha256) == (EXAMPLES / "sites/northwind-bad/index.html").read_bytes()
    for a in rows:
        assert store.verify(a.sha256)
        assert a.path == f"{a.sha256[:2]}/{a.sha256[2:4]}/{a.sha256}"
        assert a.bytes == len(store.get(a.sha256))

    with sf() as s:
        events = [
            (e.action, e.entity_id) for e in s.scalars(select(AuditLog).order_by(AuditLog.id))
        ]
        assert events[0] == ("scan.started", str(scan_id))
        assert [a for a, _ in events[1:-1]] == ["artifact.stored"] * 4
        assert events[-1] == ("scan.finished", str(scan_id))
        finished = s.scalars(select(AuditLog).where(AuditLog.action == "scan.finished")).one()
        assert finished.payload["findings"] == 2
        assert sorted(finished.payload["artifacts"]) == sorted(a.sha256 for a in rows)
        assert audit.verify_chain(s).ok


def test_findings_cite_the_scanners_basis(
    sf: sessionmaker[Session], store: ArtifactStore, site: str
) -> None:
    tp = touchpoint(sf, "page", f"{site}/sites/northwind-bad/")
    for f in findings(sf, run_scan(sf, tp, store=store, clock=ticking())):
        catalogue = scanner_api.rule_basis(f.rule_id)
        assert f.basis == catalogue  # stored verbatim, never paraphrased
        assert f.paragraph == catalogue["paragraph"]
        assert f.basis["basis"], f"{f.rule_id} has no pinpoint basis"
        assert all(b["url"].startswith("https://") for b in f.basis["basis"])


# --- transcripts and media -----------------------------------------------------------


def test_bad_transcripts_produce_int_002_and_int_003(
    sf: sessionmaker[Session], store: ArtifactStore
) -> None:
    tp = touchpoint(sf, "transcripts", str(EXAMPLES / "transcripts"))
    scan_id = run_scan(sf, tp, store=store, clock=ticking())
    assert scan_row(sf, scan_id).status == "done"
    by_file = {(Path(f.target).name, f.rule_id, f.status) for f in findings(sf, scan_id)}
    assert ("02-late-disclosure.json", "INT-002", "gap") in by_file
    assert ("03-claims-to-be-human.txt", "INT-003", "gap") in by_file
    assert ("01-discloses-up-front.json", "INT-010", "pass") in by_file

    stored = {Path(a.name).name: a for a in artifacts(sf, scan_id) if a.kind == "transcript"}
    assert set(stored) == {p.name for p in (EXAMPLES / "transcripts").iterdir()}
    for name, a in stored.items():
        assert store.get(a.sha256) == (EXAMPLES / "transcripts" / name).read_bytes()


def test_transcript_batch_findings_stay_distinct_per_conversation(
    sf: sessionmaker[Session], store: ArtifactStore
) -> None:
    tp = touchpoint(sf, "transcripts", str(EXAMPLES / "transcripts" / "batch.jsonl"))
    rows = findings(sf, run_scan(sf, tp, store=store, clock=ticking()))
    int010 = [f for f in rows if f.rule_id == "INT-010"]  # c-01 and c-03, both "message 1"
    assert len(int010) == 2
    assert len({f.fingerprint for f in int010}) == 2


def test_ui_notice_option_is_passed_to_the_scanner(
    sf: sessionmaker[Session], store: ArtifactStore
) -> None:
    target = str(EXAMPLES / "transcripts" / "02-late-disclosure.json")
    plain = findings(sf, run_scan(sf, touchpoint(sf, "transcripts", target), store=store))
    notice = findings(
        sf,
        run_scan(sf, touchpoint(sf, "transcripts", target, {"ui_notice": True}), store=store),
    )
    assert ("INT-002", "gap") in statuses(plain)
    assert ("INT-002", "gap") not in statuses(notice)


def test_media_reproduces_med_001_and_med_002(
    sf: sessionmaker[Session], store: ArtifactStore
) -> None:
    tp = touchpoint(sf, "media", str(EXAMPLES / "media"))
    scan_id = run_scan(sf, tp, store=store, clock=ticking())
    by_file = {(Path(f.target).name, f.rule_id, f.status) for f in findings(sf, scan_id)}
    assert ("generator-trace.png", "MED-001", "gap") in by_file
    assert ("marked-iptc.jpg", "MED-002", "pass") in by_file
    assert ("marked-iptc.png", "MED-002", "pass") in by_file
    media_files = {Path(a.name).name for a in artifacts(sf, scan_id) if a.kind == "media"}
    assert media_files == {p.name for p in (EXAMPLES / "media").iterdir()}


# --- determinism ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "target"),
    [
        ("page", "{site}/sites/northwind-bad/"),
        ("page", "{site}/sites/northwind-good/"),
        ("transcripts", str(EXAMPLES / "transcripts")),
        ("media", str(EXAMPLES / "media")),
    ],
)
def test_same_input_twice_gives_same_findings_and_fingerprints(
    sf: sessionmaker[Session], store: ArtifactStore, site: str, kind: str, target: str
) -> None:
    tp = touchpoint(sf, kind, target.format(site=site))
    first = run_scan(sf, tp, store=store, clock=frozen())
    second = run_scan(sf, tp, store=store, clock=frozen())

    def key(rows: list[Finding]) -> list[tuple[str, ...]]:
        return sorted(
            (f.fingerprint, f.rule_id, f.status, f.paragraph, f.location, f.detail, f.evidence)
            for f in rows
        )

    a, b = findings(sf, first), findings(sf, second)
    assert a and key(a) == key(b)
    # Same input and same clock: byte-identical evidence, so the same hashes.
    assert sorted(x.sha256 for x in artifacts(sf, first)) == sorted(
        x.sha256 for x in artifacts(sf, second)
    )
    with sf() as s:
        assert s.execute(text("SELECT count(*) FROM v_regressions")).scalar_one() == 0


# --- failures --------------------------------------------------------------------------


def test_unreachable_page_is_recorded_as_an_error(
    sf: sessionmaker[Session], store: ArtifactStore, site: str
) -> None:
    tp = touchpoint(sf, "page", f"{site}/sites/no-such-site/")
    scan_id = run_scan(sf, tp, store=store, clock=ticking())
    scan = scan_row(sf, scan_id)
    assert scan.status == "error"
    assert scan.error is not None and "HTTP 404" in scan.error
    assert scan.finished_at is not None
    assert findings(sf, scan_id) == []
    # The scanner's own report naming the failure is kept as evidence.
    assert [a.kind for a in artifacts(sf, scan_id)] == ["report_json"]
    with sf() as s:
        failed = s.scalars(select(AuditLog).where(AuditLog.action == "scan.failed")).one()
        assert "HTTP 404" in failed.payload["error"]
        assert audit.verify_chain(s).ok


class ExplodingScanner:
    """The real scanner API, except that the chosen call raises."""

    def __init__(self, fail: str = "scan_pages", exc: Exception | None = None) -> None:
        self.fail = fail
        self.exc = exc or RuntimeError("chromium crashed")

    def __getattr__(self, name: str) -> Any:
        if name == self.fail:

            def boom(*args: Any, **kwargs: Any) -> Any:
                raise self.exc

            return boom
        return getattr(scanner_api, name)


def test_scanner_exception_is_stored_not_swallowed(
    sf: sessionmaker[Session], store: ArtifactStore, site: str
) -> None:
    tp = touchpoint(sf, "page", f"{site}/sites/northwind-good/")
    scan_id = run_scan(sf, tp, store=store, clock=ticking(), scanner=ExplodingScanner())
    scan = scan_row(sf, scan_id)
    assert (scan.status, scan.error) == ("error", "RuntimeError: chromium crashed")
    assert artifacts(sf, scan_id) == []
    assert findings(sf, scan_id) == []


def test_rule_missing_from_catalogue_fails_the_scan(
    sf: sessionmaker[Session], store: ArtifactStore, site: str
) -> None:
    tp = touchpoint(sf, "page", f"{site}/sites/northwind-good/")
    scanner = ExplodingScanner("rule_basis", KeyError("PAGE-003"))
    scan = scan_row(sf, run_scan(sf, tp, store=store, scanner=scanner))
    assert scan.status == "error"
    assert scan.error is not None and "not in the scanner's catalogue" in scan.error


def test_unknown_touchpoint_option_fails_the_scan(
    sf: sessionmaker[Session], store: ArtifactStore
) -> None:
    tp = touchpoint(sf, "media", str(EXAMPLES / "media"), {"browser": True})
    scan = scan_row(sf, run_scan(sf, tp, store=store))
    assert scan.status == "error"
    assert scan.error is not None and "unknown option(s) for a media touchpoint: browser" in (
        scan.error
    )


def test_failed_scan_raises_no_regression(
    sf: sessionmaker[Session], store: ArtifactStore, site: str
) -> None:
    tp = touchpoint(sf, "transcripts", str(EXAMPLES / "transcripts"))
    clock = ticking()
    first = run_scan(sf, tp, store=store, clock=clock)
    failed = run_scan(
        sf, tp, store=store, clock=clock, scanner=ExplodingScanner("scan_transcripts")
    )
    third = run_scan(sf, tp, store=store, clock=clock)

    assert [scan_row(sf, i).status for i in (first, failed, third)] == ["done", "error", "done"]
    with sf() as s:
        assert s.execute(text("SELECT count(*) FROM v_regressions")).scalar_one() == 0
        latest = s.execute(
            text("SELECT scan_id FROM v_latest_scan WHERE touchpoint_id = :t"), {"t": tp}
        ).scalar_one()
        assert latest == third
        assert audit.verify_chain(s).ok


# --- browser mode (needs Playwright's Chromium; skipped without it) ---------------------


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            pw.chromium.launch().close()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _chromium_available(), reason="Playwright Chromium not installed")
def test_browser_scan_stores_rendered_html_and_a_screenshot(
    sf: sessionmaker[Session], store: ArtifactStore
) -> None:
    page = (FIXTURES / "local-pages" / "no-widget.html").resolve()
    tp = touchpoint(sf, "page", str(page), {"browser": True, "check_media": False})
    scan_id = run_scan(sf, tp, store=store, clock=ticking())
    assert scan_row(sf, scan_id).status == "done"
    kinds = sorted(a.kind for a in artifacts(sf, scan_id))
    assert kinds == ["html", "report_json", "screenshot"]
    shot = next(a for a in artifacts(sf, scan_id) if a.kind == "screenshot")
    assert store.get(shot.sha256).startswith(b"\x89PNG")
    assert ("PAGE-005", "info") in statuses(findings(sf, scan_id))
