"""Run one scan of one touchpoint and record everything it produced.

    run_scan(session_factory, touchpoint_id, store=..., triggered_by=...) -> scan id

Three short transactions, so the scanner (which may take a minute in browser mode)
never runs inside an open transaction:

1. insert the ``scans`` row as ``running`` and audit ``scan.started``; commit.
2. run ai-disclosure-scan through its Python API (no transaction open).
3. store every evidence file and the full ``report.json`` by SHA-256, insert
   ``artifacts`` and ``findings`` (with fingerprints and the rule's legal basis), mark
   the scan ``done`` and audit ``artifact.stored`` per file and ``scan.finished``;
   commit.

If the scanner raises, or reports an error for any target, or step 3 fails, the
scan is marked ``error`` with the message and audited as ``scan.failed``. Errors are
recorded, never swallowed. A failed scan has no findings, so it can never produce a
regression (the views only compare ``done`` scans). Artifacts that the scanner did
return before failing (for instance its report.json naming the error) are still kept.
See docs/decisions.md, D-014.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from ai_disclosure_scan import api as scanner_api
from sqlalchemy.orm import Session, sessionmaker

from evidence_room import audit, notify
from evidence_room.config import get_settings
from evidence_room.fingerprint import fingerprint, subject_for
from evidence_room.models import Artifact, Finding, Scan, Touchpoint
from evidence_room.store import ArtifactStore

log = logging.getLogger("evidence_room.runner")

ACTOR = "system:runner"
Clock = Callable[[], datetime]

# Touchpoint options each kind accepts, and the scanner keyword each maps to.
PAGE_OPTIONS = {
    "browser": "browser",
    "accept_cookies": "accept_cookies",
    "follow_contact": "follow_contact",
    "check_media": "check_media",
    "max_media": "max_media",
    "timeout": "timeout",
    "respect_robots": "respect_robots",
    "screenshot": "screenshot",
}
TRANSCRIPT_OPTIONS = {"ui_notice": "ui_notice"}
MEDIA_OPTIONS: dict[str, str] = {}

EVIDENCE_KINDS = {
    "html": "html",
    "screenshot": "screenshot",
    "media": "media",
    "transcript": "transcript",
}


class ScannerAPI(Protocol):
    def scanner_version(self) -> str: ...
    def rule_basis(self, rule_id: str) -> dict[str, Any]: ...
    def scan_pages(self, targets: Iterable[str], **kw: Any) -> Any: ...
    def scan_transcripts(self, paths: Iterable[str], **kw: Any) -> Any: ...
    def scan_media(self, paths: Iterable[str], **kw: Any) -> Any: ...


class ScanError(Exception):
    """The scan could not produce trustworthy findings. The message is stored on the scan."""


class _NoLongerRunning(Exception):
    """The scan row was finished by someone else (the worker's stale-scan reaper)."""


def _lock_running_scan(session: Session, scan_id: int) -> Scan:
    scan = session.get(Scan, scan_id, with_for_update=True)
    assert scan is not None
    if scan.status != "running":
        raise _NoLongerRunning(f"scan {scan_id} is already {scan.status}")
    return scan


@dataclass(frozen=True)
class _Outcome:
    result: Any  # ai_disclosure_scan.api.ScanResult
    finished: datetime


def utcnow() -> datetime:
    return datetime.now(UTC)


def scanner_kwargs(touchpoint: Touchpoint) -> dict[str, Any]:
    allowed = {"page": PAGE_OPTIONS, "transcripts": TRANSCRIPT_OPTIONS, "media": MEDIA_OPTIONS}[
        touchpoint.kind
    ]
    options = dict(touchpoint.options or {})
    unknown = sorted(set(options) - set(allowed))
    if unknown:
        raise ScanError(
            f"unknown option(s) for a {touchpoint.kind} touchpoint: {', '.join(unknown)} "
            f"(allowed: {', '.join(sorted(allowed)) or 'none'})"
        )
    return {allowed[k]: v for k, v in options.items()}


def run_scan(
    session_factory: sessionmaker[Session],
    touchpoint_id: int,
    *,
    store: ArtifactStore,
    triggered_by: str = "manual",
    clock: Clock = utcnow,
    scanner: ScannerAPI = scanner_api,  # type: ignore[assignment]
    base_url: str | None = None,
) -> int:
    """Scan one touchpoint now. Returns the new scan's id, whatever its outcome.

    The scan's events (regressions, resolutions, or the failure) are queued for
    notification in the same transaction that records the outcome (see notify.py).
    """
    base_url = base_url or get_settings().app_base_url
    with session_factory() as session, session.begin():
        touchpoint = session.get(Touchpoint, touchpoint_id)
        if touchpoint is None:
            raise LookupError(f"touchpoint {touchpoint_id} does not exist")
        scan = Scan(
            touchpoint_id=touchpoint.id,
            started_at=clock(),
            scanner_version=scanner.scanner_version(),
            status="running",
            triggered_by=triggered_by,
        )
        session.add(scan)
        session.flush()
        audit.append(
            session,
            ACTOR,
            "scan.started",
            "scan",
            scan.id,
            {
                "touchpoint_id": touchpoint.id,
                "kind": touchpoint.kind,
                "target": touchpoint.target,
                "triggered_by": triggered_by,
                "scanner_version": scan.scanner_version,
            },
            clock=clock,
        )
        scan_id, kind, target = scan.id, touchpoint.kind, touchpoint.target
        tp_snapshot = Touchpoint(
            id=touchpoint.id, kind=kind, target=target, options=dict(touchpoint.options or {})
        )

    outcome: _Outcome | None = None
    try:
        outcome = _call_scanner(scanner, tp_snapshot, clock)
        _record_success(
            session_factory, scan_id, tp_snapshot, outcome, store, scanner, clock, base_url
        )
    except _NoLongerRunning as exc:
        log.warning("not recording scan %s: %s", scan_id, exc)
        return scan_id
    except Exception as exc:  # every failure is recorded on the scan
        message = str(exc) if isinstance(exc, ScanError) else f"{type(exc).__name__}: {exc}"
        log.warning("scan %s of touchpoint %s failed: %s", scan_id, tp_snapshot.id, message)
        _record_failure(
            session_factory, scan_id, tp_snapshot, message, outcome, store, clock, base_url
        )
    return scan_id


def _call_scanner(scanner: ScannerAPI, tp: Touchpoint, clock: Clock) -> _Outcome:
    kwargs = scanner_kwargs(tp)
    when = clock()
    if tp.kind == "page":
        result = scanner.scan_pages([tp.target], generated_at=when, **kwargs)
    elif tp.kind == "transcripts":
        result = scanner.scan_transcripts([tp.target], generated_at=when, **kwargs)
    elif tp.kind == "media":
        result = scanner.scan_media([tp.target], generated_at=when, **kwargs)
    else:  # pragma: no cover - the CHECK constraint makes this unreachable
        raise ScanError(f"unknown touchpoint kind {tp.kind!r}")
    return _Outcome(result, clock())


def _store_artifacts(
    session: Session,
    scan_id: int,
    outcome: _Outcome,
    store: ArtifactStore,
    clock: Clock,
) -> list[Artifact]:
    items: list[tuple[str, str, bytes, str]] = [
        (EVIDENCE_KINDS[e.kind], e.name, e.data, e.media_type) for e in outcome.result.evidence
    ]
    items.append(
        ("report_json", "report.json", outcome.result.to_json().encode("utf-8"), "application/json")
    )
    rows: list[Artifact] = []
    seen: set[tuple[str, str, str]] = set()
    for kind, name, data, media_type in items:
        blob = store.put(data)
        key = (kind, name, blob.sha256)
        if key in seen:  # the same file referenced twice on one page
            continue
        seen.add(key)
        row = Artifact(
            scan_id=scan_id,
            kind=kind,
            name=name,
            media_type=media_type,
            sha256=blob.sha256,
            bytes=blob.size,
            path=blob.relpath,
            captured_at=outcome.finished,
        )
        session.add(row)
        session.flush()
        audit.append(
            session,
            ACTOR,
            "artifact.stored",
            "artifact",
            row.id,
            {
                "scan_id": scan_id,
                "kind": kind,
                "name": name,
                "sha256": blob.sha256,
                "bytes": blob.size,
            },
            clock=clock,
        )
        rows.append(row)
    return rows


def _findings(
    scan_id: int, tp: Touchpoint, outcome: _Outcome, scanner: ScannerAPI
) -> list[Finding]:
    rows: list[Finding] = []
    for target_result, f in outcome.result.report.all_findings():
        try:
            basis = scanner.rule_basis(f.rule_id)
        except KeyError as exc:
            # Rule 5: every finding cites its basis. A finding whose rule is not in the
            # scanner's own catalogue cannot, so the whole scan is refused.
            raise ScanError(f"rule {f.rule_id} is not in the scanner's catalogue") from exc
        subject = subject_for(tp.kind, tp.target, target_result.target)
        rows.append(
            Finding(
                scan_id=scan_id,
                target=target_result.target,
                rule_id=f.rule_id,
                paragraph=str(basis.get("paragraph", "")),
                status=f.status.value,
                detail=f.detail,
                evidence=f.evidence,
                location=f.location,
                basis=basis,
                fingerprint=fingerprint(tp.id, f.rule_id, subject, f.location),
            )
        )
    return rows


def findings_digest(rows: Iterable[Finding]) -> str:
    """One hash over every finding of a scan, recorded in the audit chain."""
    canonical = sorted(
        json.dumps(
            [r.fingerprint, r.rule_id, r.status, r.target, r.location, r.detail, r.evidence],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        for r in rows
    )
    return hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()


def _target_errors(outcome: _Outcome) -> list[str]:
    return [f"{r.target}: {r.error}" for r in outcome.result.report.results if r.error]


def _record_success(
    session_factory: sessionmaker[Session],
    scan_id: int,
    tp: Touchpoint,
    outcome: _Outcome,
    store: ArtifactStore,
    scanner: ScannerAPI,
    clock: Clock,
    base_url: str,
) -> None:
    errors = _target_errors(outcome)
    if errors:
        raise ScanError("scanner reported errors: " + "; ".join(errors))
    if not outcome.result.report.results:
        raise ScanError("scanner returned no results")
    with session_factory() as session, session.begin():
        scan = _lock_running_scan(session, scan_id)
        artifacts = _store_artifacts(session, scan_id, outcome, store, clock)
        findings = _findings(scan_id, tp, outcome, scanner)
        session.add_all(findings)
        scan.status = "done"
        scan.finished_at = outcome.finished
        session.flush()
        counts = Counter(f.status for f in findings)
        audit.append(
            session,
            ACTOR,
            "scan.finished",
            "scan",
            scan_id,
            {
                "touchpoint_id": tp.id,
                "findings": len(findings),
                "by_status": dict(sorted(counts.items())),
                "findings_digest": findings_digest(findings),
                "artifacts": [a.sha256 for a in artifacts],
            },
            clock=clock,
        )
        notify.queue_events(session, scan_id, clock=clock, base_url=base_url)


def _record_failure(
    session_factory: sessionmaker[Session],
    scan_id: int,
    tp: Touchpoint,
    message: str,
    outcome: _Outcome | None,
    store: ArtifactStore,
    clock: Clock,
    base_url: str,
) -> None:
    with session_factory() as session, session.begin():
        try:
            scan = _lock_running_scan(session, scan_id)
        except _NoLongerRunning as exc:
            log.warning("not recording failure of scan %s: %s", scan_id, exc)
            return
        artifacts: list[Artifact] = []
        if outcome is not None:
            try:
                with session.begin_nested():
                    artifacts = _store_artifacts(session, scan_id, outcome, store, clock)
            except Exception as exc:
                log.warning("could not keep artifacts of failed scan %s: %s", scan_id, exc)
                artifacts = []
        scan.status = "error"
        scan.error = message
        scan.finished_at = outcome.finished if outcome is not None else clock()
        session.flush()
        audit.append(
            session,
            ACTOR,
            "scan.failed",
            "scan",
            scan_id,
            {"touchpoint_id": tp.id, "error": message, "artifacts": [a.sha256 for a in artifacts]},
            clock=clock,
        )
        notify.queue_events(session, scan_id, clock=clock, base_url=base_url)
