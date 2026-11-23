"""Small deterministic builders for test rows. Fixed timestamps, no randomness."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from evidence_room.models import (
    Artifact,
    Client,
    Finding,
    Matter,
    Review,
    Scan,
    Touchpoint,
    User,
)

T0 = datetime(2026, 8, 3, 9, 0, tzinfo=UTC)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def user(session: Session, email: str = "lawyer@example.test", role: str = "lawyer") -> User:
    row = User(email=email, name=email.split("@")[0], role=role, password_hash="x")
    session.add(row)
    session.flush()
    return row


def touchpoint(session: Session, client_name: str = "Northwind Energy") -> Touchpoint:
    client = Client(name=client_name)
    session.add(client)
    session.flush()
    matter = Matter(client_id=client.id, title="Art. 50 readiness", retention_days=365)
    session.add(matter)
    session.flush()
    tp = Touchpoint(
        matter_id=matter.id,
        kind="page",
        target="http://fixtures-site/sites/northwind-good/",
        party_role="deployer",
    )
    session.add(tp)
    session.flush()
    return tp


def scan(session: Session, tp: Touchpoint, minutes: int = 0, status: str = "done") -> Scan:
    at = T0 + timedelta(minutes=minutes)
    row = Scan(
        touchpoint_id=tp.id,
        started_at=at,
        finished_at=at + timedelta(seconds=5) if status in ("done", "error") else None,
        scanner_version="0.4.0",
        status=status,
        error="boom" if status == "error" else None,
        triggered_by="test",
    )
    session.add(row)
    session.flush()
    return row


def finding(
    session: Session,
    sc: Scan,
    rule_id: str = "INT-001",
    status: str = "gap",
    location: str = "",
    paragraph: str = "50(1)",
) -> Finding:
    row = Finding(
        scan_id=sc.id,
        target="http://fixtures-site/sites/northwind-good/",
        rule_id=rule_id,
        paragraph=paragraph,
        status=status,
        detail=f"{rule_id} detail",
        evidence="",
        location=location,
        basis={"article": "Art. 50(1)", "text": "basis text from the scanner"},
        fingerprint=sha(f"{sc.touchpoint_id}|{rule_id}|{location}"),
    )
    session.add(row)
    session.flush()
    return row


def review(
    session: Session,
    f: Finding,
    reviewer: User,
    decision: str = "confirm",
    reason: str | None = None,
    minutes: int = 0,
) -> Review:
    row = Review(
        finding_id=f.id,
        reviewer_id=reviewer.id,
        decision=decision,
        reason=reason,
        created_at=T0 + timedelta(hours=1, minutes=minutes),
    )
    session.add(row)
    session.flush()
    return row


def artifact(session: Session, sc: Scan, content: bytes = b"<html></html>") -> Artifact:
    digest = hashlib.sha256(content).hexdigest()
    row = Artifact(
        scan_id=sc.id,
        kind="html",
        sha256=digest,
        bytes=len(content),
        path=f"{digest[:2]}/{digest[2:4]}/{digest}",
        captured_at=sc.started_at or T0,
    )
    session.add(row)
    session.flush()
    return row
