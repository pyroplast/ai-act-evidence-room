"""SQLAlchemy models. The schema itself is owned by Alembic (alembic/versions); these
classes mirror it, and tests/test_schema.py fails if the two drift apart.

Enumerations are TEXT columns with CHECK constraints (docs/decisions.md, D-004).
Integrity rules that the ORM cannot express (immutability, append-only, hash-chain
linkage) live in sql/triggers.sql.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

USER_ROLES = ("lawyer", "analyst", "admin")
MATTER_STATUSES = ("open", "on_hold", "closed")
TOUCHPOINT_KINDS = ("page", "transcripts", "media")
PARTY_ROLES = ("provider", "deployer")
SCHEDULES = ("daily", "weekly", "manual")
SCAN_STATUSES = ("queued", "running", "done", "error")
ARTIFACT_KINDS = ("html", "screenshot", "transcript", "media", "report_json")
# The scanner's own vocabulary (ai_disclosure_scan.models.Status).
FINDING_STATUSES = ("gap", "review", "pass", "info")
REVIEW_DECISIONS = (
    "confirm",
    "override_pass",
    "override_gap",
    "not_applicable",
    "needs_client_input",
)

SHA256_RE = "^[0-9a-f]{64}$"


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


def _ts(**kw: Any) -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), **kw)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint(_in("role", USER_ROLES), name="ck_users_role"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class Client(Base):
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    notes: Mapped[str] = mapped_column(Text, server_default=text("''"))


class Matter(Base):
    __tablename__ = "matters"
    __table_args__ = (
        CheckConstraint(_in("status", MATTER_STATUSES), name="ck_matters_status"),
        CheckConstraint(
            "retention_days IS NULL OR retention_days > 0", name="ck_matters_retention_days"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("clients.id"), index=True)
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'open'"))
    # NULL means "keep until purged by hand".
    retention_days: Mapped[int | None] = mapped_column(Integer)
    opened_at: Mapped[datetime] = _ts(server_default=func.now())


class Touchpoint(Base):
    __tablename__ = "touchpoints"
    __table_args__ = (
        CheckConstraint(_in("kind", TOUCHPOINT_KINDS), name="ck_touchpoints_kind"),
        CheckConstraint(_in("party_role", PARTY_ROLES), name="ck_touchpoints_party_role"),
        CheckConstraint(_in("schedule", SCHEDULES), name="ck_touchpoints_schedule"),
        Index("ix_touchpoints_due", "next_run_at", postgresql_where=text("active")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    matter_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matters.id"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text)
    party_role: Mapped[str] = mapped_column(Text)
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    schedule: Mapped[str] = mapped_column(Text, server_default=text("'manual'"))
    next_run_at: Mapped[datetime | None] = _ts()
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Scan(Base):
    __tablename__ = "scans"
    __table_args__ = (
        CheckConstraint(_in("status", SCAN_STATUSES), name="ck_scans_status"),
        CheckConstraint(
            "status NOT IN ('done', 'error') OR finished_at IS NOT NULL",
            name="ck_scans_finished_at",
        ),
        Index("ix_scans_touchpoint_finished", "touchpoint_id", "finished_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    touchpoint_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("touchpoints.id"))
    started_at: Mapped[datetime | None] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
    scanner_version: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'queued'"))
    error: Mapped[str | None] = mapped_column(Text)
    triggered_by: Mapped[str] = mapped_column(Text)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        CheckConstraint(_in("kind", ARTIFACT_KINDS), name="ck_artifacts_kind"),
        CheckConstraint(f"sha256 ~ '{SHA256_RE}'", name="ck_artifacts_sha256"),
        CheckConstraint("bytes >= 0", name="ck_artifacts_bytes"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("scans.id"), index=True)
    kind: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    path: Mapped[str] = mapped_column(Text)
    captured_at: Mapped[datetime] = _ts()
    purged_at: Mapped[datetime | None] = _ts()


class Finding(Base):
    __tablename__ = "findings"
    __table_args__ = (
        CheckConstraint(_in("status", FINDING_STATUSES), name="ck_findings_status"),
        CheckConstraint(f"fingerprint ~ '{SHA256_RE}'", name="ck_findings_fingerprint"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    scan_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("scans.id"), index=True)
    # The scanned thing within the touchpoint (a page URL, one conversation, one file).
    # Not in PLAN.md's table; added so transcript batches stay distinguishable (D-009).
    target: Mapped[str] = mapped_column(Text, server_default=text("''"))
    rule_id: Mapped[str] = mapped_column(Text)
    paragraph: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    evidence: Mapped[str] = mapped_column(Text, server_default=text("''"))
    location: Mapped[str] = mapped_column(Text, server_default=text("''"))
    basis: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    fingerprint: Mapped[str] = mapped_column(String(64), index=True)


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        CheckConstraint(_in("decision", REVIEW_DECISIONS), name="ck_reviews_decision"),
        # Rule 3: anything other than a plain confirm needs a written reason.
        CheckConstraint(
            "decision = 'confirm' OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="ck_reviews_reason_required",
        ),
        Index("ix_reviews_finding_created", "finding_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    finding_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("findings.id"))
    reviewer_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (
        CheckConstraint(f"prev_hash ~ '{SHA256_RE}'", name="ck_audit_log_prev_hash"),
        CheckConstraint(f"hash ~ '{SHA256_RE}'", name="ck_audit_log_hash"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = _ts()
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64), unique=True)


class Webhook(Base):
    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    url: Mapped[str] = mapped_column(Text)
    secret: Mapped[str] = mapped_column(Text)
    events: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=text("'{}'::text[]"))
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (
        Index(
            "ix_deliveries_pending",
            "next_attempt_at",
            postgresql_where=text("delivered_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    webhook_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("webhooks.id"), index=True)
    event: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attempt: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    status_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts(server_default=func.now())
    next_attempt_at: Mapped[datetime | None] = _ts()
    delivered_at: Mapped[datetime | None] = _ts()
