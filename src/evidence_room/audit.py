"""Hash-chained, append-only audit log.

Every row stores ``prev_hash`` (the previous row's ``hash``, or 64 zeros for the first
row) and ``hash = sha256(prev_hash || canonical_json(record))`` where ``record`` is

    {"action", "actor", "at", "entity", "entity_id", "payload"}

serialised with sorted keys, no whitespace, UTF-8, and ``at`` as an ISO-8601 UTC
timestamp with microseconds. Postgres refuses UPDATE, DELETE and TRUNCATE on the table
and checks on INSERT that ``prev_hash`` links to the current head (sql/triggers.sql).

``append`` runs on the caller's session, so the audit row commits or rolls back with the
change it records. It takes a transaction-scoped advisory lock first, so two concurrent
writers cannot both link to the same head.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from evidence_room.models import AuditLog

GENESIS_HASH = "0" * 64
# Arbitrary constant key for pg_advisory_xact_lock; serialises appends to the chain.
AUDIT_LOCK_KEY = 0x45_56_52_4D_41_55_44  # "EVRMAUD"

Clock = Callable[[], datetime]


def utcnow() -> datetime:
    return datetime.now(UTC)


def _reject_floats(value: Any, path: str = "payload") -> None:
    # Floats do not round-trip reliably through jsonb, which would make a correct
    # chain fail verification. Payloads carry ints and strings only.
    if isinstance(value, float):
        raise TypeError(f"{path}: floats are not allowed in audit payloads; use str or int")
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise TypeError(f"{path}: keys must be strings")
            _reject_floats(v, f"{path}.{k}")
    elif isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _reject_floats(v, f"{path}[{i}]")


def format_at(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("audit timestamps must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def canonical_json(
    *, at: datetime, actor: str, action: str, entity: str, entity_id: str, payload: dict[str, Any]
) -> str:
    record = {
        "action": action,
        "actor": actor,
        "at": format_at(at),
        "entity": entity,
        "entity_id": entity_id,
        "payload": payload,
    }
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def append(
    session: Session,
    actor: str,
    action: str,
    entity: str,
    entity_id: str | int,
    payload: dict[str, Any] | None = None,
    *,
    clock: Clock = utcnow,
) -> AuditLog:
    """Add one event to the chain inside the caller's transaction. Does not commit."""
    payload = dict(payload or {})
    _reject_floats(payload)
    entity_id = str(entity_id)

    session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": AUDIT_LOCK_KEY})
    head = session.execute(
        select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)
    ).scalar_one_or_none()
    prev_hash = head or GENESIS_HASH

    # Python datetimes and timestamptz both carry microseconds, so this round-trips.
    at = clock().astimezone(UTC)
    canonical = canonical_json(
        at=at, actor=actor, action=action, entity=entity, entity_id=entity_id, payload=payload
    )
    row = AuditLog(
        at=at,
        actor=actor,
        action=action,
        entity=entity,
        entity_id=entity_id,
        payload=payload,
        prev_hash=prev_hash,
        hash=compute_hash(prev_hash, canonical),
    )
    session.add(row)
    session.flush()
    return row


@dataclass(frozen=True)
class ChainResult:
    ok: bool
    rows_checked: int
    head_hash: str
    failed_id: int | None = None
    reason: str = ""

    def describe(self) -> str:
        if self.ok:
            return f"audit chain OK: {self.rows_checked} rows, head {self.head_hash}"
        return f"audit chain BROKEN at row {self.failed_id}: {self.reason}"


def verify_chain(session: Session, *, batch_size: int = 1000) -> ChainResult:
    """Walk the whole chain in id order and recompute every hash.

    Stops at the first row whose ``prev_hash`` does not link to the previous row or whose
    stored ``hash`` does not match its contents, and names that row.
    """
    expected_prev = GENESIS_HASH
    checked = 0
    rows = session.execute(
        select(
            AuditLog.id,
            AuditLog.at,
            AuditLog.actor,
            AuditLog.action,
            AuditLog.entity,
            AuditLog.entity_id,
            AuditLog.payload,
            AuditLog.prev_hash,
            AuditLog.hash,
        )
        .order_by(AuditLog.id)
        .execution_options(yield_per=batch_size)
    )
    for row in rows:
        if row.prev_hash != expected_prev:
            return ChainResult(
                False,
                checked,
                expected_prev,
                row.id,
                f"prev_hash {row.prev_hash} does not link to previous hash {expected_prev}",
            )
        canonical = canonical_json(
            at=row.at,
            actor=row.actor,
            action=row.action,
            entity=row.entity,
            entity_id=row.entity_id,
            payload=row.payload,
        )
        recomputed = compute_hash(row.prev_hash, canonical)
        if recomputed != row.hash:
            return ChainResult(
                False,
                checked,
                expected_prev,
                row.id,
                f"stored hash {row.hash} != recomputed {recomputed} (row contents changed)",
            )
        expected_prev = row.hash
        checked += 1
    return ChainResult(True, checked, expected_prev)
