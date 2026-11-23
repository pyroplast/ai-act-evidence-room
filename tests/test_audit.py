"""M1 acceptance (b) and (c): the audit chain verifies, and a direct edit is caught."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from evidence_room import audit
from evidence_room.models import AuditLog
from tests.conftest import drop_database, migrate, recreate_database

FROZEN = datetime(2026, 8, 3, 9, 0, 0, 123456, tzinfo=UTC)


def frozen_clock(start: datetime = FROZEN) -> audit.Clock:
    ticks = iter(range(10_000))
    return lambda: start + timedelta(seconds=next(ticks))


def append_n(session: Session, n: int) -> list[AuditLog]:
    clock = frozen_clock()
    return [
        audit.append(
            session,
            actor="analyst@example.test",
            action="finding.reviewed",
            entity="finding",
            entity_id=i,
            payload={"decision": "confirm", "n": i, "note": "ünïcode ok"},
            clock=clock,
        )
        for i in range(1, n + 1)
    ]


def tamper(session: Session, sql: str, **params: object) -> None:
    """Edit audit_log behind the trigger's back, the way a DBA with owner rights could."""
    session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete"))
    session.execute(text(sql), params)
    session.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update_delete"))


def test_empty_chain_verifies(session: Session) -> None:
    result = audit.verify_chain(session)
    assert result.ok
    assert result.rows_checked == 0
    assert result.head_hash == audit.GENESIS_HASH


def test_hash_format_is_stable() -> None:
    """Golden value: changing the canonical form silently would orphan every stored chain."""
    canonical = audit.canonical_json(
        at=FROZEN,
        actor="system",
        action="matter.opened",
        entity="matter",
        entity_id="1",
        payload={"title": "Art. 50", "b": [1, 2], "a": None},
    )
    assert canonical == (
        '{"action":"matter.opened","actor":"system","at":"2026-08-03T09:00:00.123456+00:00",'
        '"entity":"matter","entity_id":"1","payload":{"a":null,"b":[1,2],"title":"Art. 50"}}'
    )
    assert audit.compute_hash(audit.GENESIS_HASH, canonical) == (
        "0c80678bdb4b6f3ffdd0cb752cb4f64e2cbb8cc9d8f060d115bcfbb9a3b8aa95"
    )


def test_append_links_rows_and_chain_verifies(session: Session) -> None:
    rows = append_n(session, 5)
    assert rows[0].prev_hash == audit.GENESIS_HASH
    for prev, row in pairwise(rows):
        assert row.prev_hash == prev.hash
    result = audit.verify_chain(session)
    assert result.ok, result.describe()
    assert result.rows_checked == 5
    assert result.head_hash == rows[-1].hash


def test_same_inputs_give_same_hashes(session: Session) -> None:
    first = [r.hash for r in append_n(session, 3)]
    session.rollback()  # back to the start of the test's savepoint
    second = [r.hash for r in append_n(session, 3)]
    assert first == second


def test_append_rolls_back_with_the_change_it_records(session: Session) -> None:
    append_n(session, 2)
    with pytest.raises(RuntimeError), session.begin_nested():
        audit.append(session, "system", "matter.opened", "matter", 99, {"title": "x"})
        raise RuntimeError("the change failed")
    assert session.scalar(select(func.count()).select_from(AuditLog)) == 2
    assert audit.verify_chain(session).ok


def test_payload_floats_are_rejected(session: Session) -> None:
    with pytest.raises(TypeError, match="floats are not allowed"):
        audit.append(session, "system", "x", "matter", 1, {"score": {"value": 0.5}})


def test_naive_timestamps_are_rejected(session: Session) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        audit.canonical_json(
            at=datetime(2026, 8, 3), actor="a", action="b", entity="c", entity_id="1", payload={}
        )


@pytest.mark.parametrize(
    ("column_sql", "params"),
    [
        ("payload = jsonb_set(payload, '{decision}', '\"override_pass\"')", {}),
        ("actor = 'mallory@example.test'", {}),
        ("at = at + interval '1 second'", {}),
        ("entity_id = '999'", {}),
    ],
)
def test_editing_one_row_fails_verification_at_that_row(
    session: Session, column_sql: str, params: dict[str, object]
) -> None:
    rows = append_n(session, 6)
    victim = rows[2]
    tamper(session, f"UPDATE audit_log SET {column_sql} WHERE id = :id", id=victim.id, **params)
    session.expire_all()

    result = audit.verify_chain(session)
    assert not result.ok
    assert result.failed_id == victim.id
    assert result.rows_checked == 2  # rows before the victim were fine
    assert "row contents changed" in result.reason
    assert f"row {victim.id}" in result.describe()


def test_rehashing_an_edited_row_breaks_the_next_link(session: Session) -> None:
    """Recomputing the edited row's own hash does not help: the next row no longer links."""
    rows = append_n(session, 4)
    victim = rows[1]
    forged_payload = {"decision": "override_pass", "n": 2, "note": "ünïcode ok"}
    forged_hash = audit.compute_hash(
        victim.prev_hash,
        audit.canonical_json(
            at=victim.at,
            actor=victim.actor,
            action=victim.action,
            entity=victim.entity,
            entity_id=victim.entity_id,
            payload=forged_payload,
        ),
    )
    session.execute(text("ALTER TABLE audit_log DISABLE TRIGGER audit_log_no_update_delete"))
    session.execute(
        text("UPDATE audit_log SET payload = CAST(:p AS jsonb), hash = :h WHERE id = :id"),
        {
            "p": '{"decision": "override_pass", "n": 2, "note": "ünïcode ok"}',
            "h": forged_hash,
            "id": victim.id,
        },
    )
    session.execute(text("ALTER TABLE audit_log ENABLE TRIGGER audit_log_no_update_delete"))
    session.expire_all()

    result = audit.verify_chain(session)
    assert not result.ok
    assert result.failed_id == rows[2].id
    assert "does not link" in result.reason


def test_deleting_a_middle_row_is_caught(session: Session) -> None:
    rows = append_n(session, 5)
    tamper(session, "DELETE FROM audit_log WHERE id = :id", id=rows[2].id)
    result = audit.verify_chain(session)
    assert not result.ok
    assert result.failed_id == rows[3].id


# --- concurrency: needs committed data, so it gets its own throwaway database --------


@pytest.fixture
def scratch_db_url() -> Iterator[str]:
    name = "evidence_room_test_concurrency"
    url = recreate_database(name)
    migrate(url)
    yield url
    drop_database(name)


def test_concurrent_appends_never_fork_the_chain(scratch_db_url: str) -> None:
    engine = create_engine(scratch_db_url, pool_size=8)
    writers, per_writer = 6, 15
    errors: list[BaseException] = []
    start = threading.Barrier(writers)

    def write(worker: int) -> None:
        try:
            start.wait()
            for i in range(per_writer):
                with Session(engine) as s, s.begin():
                    audit.append(s, f"worker-{worker}", "scan.finished", "scan", f"{worker}-{i}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    with Session(engine) as s:
        result = audit.verify_chain(s)
    engine.dispose()
    assert result.ok, result.describe()
    assert result.rows_checked == writers * per_writer
