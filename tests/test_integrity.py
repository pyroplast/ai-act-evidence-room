"""M1 acceptance (a): Postgres itself refuses changes to protected tables."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from evidence_room import audit
from tests import factories as fx


def refused(session: Session, sql: str, **params: object) -> str:
    """Run ``sql`` in a savepoint, assert the database raised, return the message."""
    with pytest.raises(DBAPIError) as exc_info, session.begin_nested():
        session.execute(text(sql), params)
    return str(exc_info.value.orig)


@pytest.fixture
def graph(session: Session) -> dict[str, int]:
    tp = fx.touchpoint(session)
    sc = fx.scan(session, tp)
    f = fx.finding(session, sc)
    lawyer = fx.user(session)
    r = fx.review(session, f, lawyer, "override_pass", reason="Widget shows a notice on open.")
    a = fx.artifact(session, sc)
    ev = audit.append(session, "test", "finding.created", "finding", f.id, {"rule": "INT-001"})
    return {"finding": f.id, "review": r.id, "artifact": a.id, "audit_log": ev.id}


@pytest.mark.parametrize(
    ("table", "assignment"),
    [
        ("findings", "status = 'pass'"),
        ("reviews", "decision = 'confirm'"),
        ("audit_log", "actor = 'mallory'"),
    ],
)
def test_update_is_refused(
    session: Session, graph: dict[str, int], table: str, assignment: str
) -> None:
    key = {"findings": "finding", "reviews": "review", "audit_log": "audit_log"}[table]
    msg = refused(session, f"UPDATE {table} SET {assignment} WHERE id = :id", id=graph[key])
    assert f"UPDATE on {table} is not allowed" in msg


@pytest.mark.parametrize("table", ["findings", "reviews", "audit_log"])
def test_delete_is_refused(session: Session, graph: dict[str, int], table: str) -> None:
    key = {"findings": "finding", "reviews": "review", "audit_log": "audit_log"}[table]
    # Row triggers fire before foreign-key checks, so this is the trigger refusing.
    msg = refused(session, f"DELETE FROM {table} WHERE id = :id", id=graph[key])
    assert "not allowed" in msg
    count = session.execute(
        text(f"SELECT count(*) FROM {table} WHERE id = :id"), {"id": graph[key]}
    )
    assert count.scalar_one() == 1


@pytest.mark.parametrize("table", ["findings", "reviews", "audit_log", "artifacts"])
def test_truncate_is_refused(session: Session, graph: dict[str, int], table: str) -> None:
    msg = refused(session, f"TRUNCATE {table} CASCADE")
    assert "TRUNCATE on" in msg
    assert "not allowed" in msg


def test_artifact_may_be_marked_purged_once(session: Session, graph: dict[str, int]) -> None:
    aid = graph["artifact"]
    session.execute(
        text("UPDATE artifacts SET purged_at = :t WHERE id = :id"),
        {"t": fx.T0 + timedelta(days=400), "id": aid},
    )
    row = session.execute(
        text("SELECT purged_at, sha256 FROM artifacts WHERE id = :id"), {"id": aid}
    ).one()
    assert row.purged_at is not None
    assert len(row.sha256) == 64  # the hash survives the purge

    msg = refused(
        session,
        "UPDATE artifacts SET purged_at = :t WHERE id = :id",
        t=fx.T0 + timedelta(days=500),
        id=aid,
    )
    assert "already purged" in msg


@pytest.mark.parametrize(
    "assignment",
    [
        "sha256 = repeat('a', 64)",
        "path = 'elsewhere'",
        "bytes = 1",
        "purged_at = now(), path = 'elsewhere'",  # purge plus a sneaky edit
        "purged_at = NULL",
    ],
)
def test_artifact_other_updates_are_refused(
    session: Session, graph: dict[str, int], assignment: str
) -> None:
    msg = refused(
        session, f"UPDATE artifacts SET {assignment} WHERE id = :id", id=graph["artifact"]
    )
    assert "may only set purged_at" in msg


def test_artifact_delete_is_refused(session: Session, graph: dict[str, int]) -> None:
    msg = refused(session, "DELETE FROM artifacts WHERE id = :id", id=graph["artifact"])
    assert "DELETE on artifacts is not allowed" in msg


@pytest.mark.parametrize(
    "decision", ["override_pass", "override_gap", "not_applicable", "needs_client_input"]
)
@pytest.mark.parametrize("reason", [None, "", "   "])
def test_review_without_reason_is_rejected(
    session: Session, decision: str, reason: str | None
) -> None:
    tp = fx.touchpoint(session)
    f = fx.finding(session, fx.scan(session, tp))
    lawyer = fx.user(session)
    with pytest.raises(IntegrityError) as exc_info, session.begin_nested():
        fx.review(session, f, lawyer, decision, reason=reason)
    assert "ck_reviews_reason_required" in str(exc_info.value.orig)


def test_confirm_needs_no_reason(session: Session) -> None:
    tp = fx.touchpoint(session)
    f = fx.finding(session, fx.scan(session, tp))
    r = fx.review(session, f, fx.user(session), "confirm", reason=None)
    assert r.id is not None


def test_audit_insert_must_link_to_head(session: Session, graph: dict[str, int]) -> None:
    msg = refused(
        session,
        """
        INSERT INTO audit_log (at, actor, action, entity, entity_id, payload, prev_hash, hash)
        VALUES (now(), 'mallory', 'forged', 'finding', '1', '{}', :prev, :hash)
        """,
        prev="0" * 64,  # not the head: graph already appended one event
        hash="f" * 64,
    )
    assert "does not link to head" in msg
