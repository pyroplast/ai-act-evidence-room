"""Schema: every table in PLAN.md's data model.

Revision ID: 0002_schema
Revises: 0001_baseline
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql as pg

revision: str = "0002_schema"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHA256_RE = "^[0-9a-f]{64}$"


def _in(column: str, *values: str) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def _ts(name: str, nullable: bool = True, **kw: object) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable, **kw)


def _id() -> sa.Column:
    return sa.Column("id", sa.BigInteger(), primary_key=True)


def upgrade() -> None:
    op.create_table(
        "users",
        _id(),
        sa.Column("email", sa.Text(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(_in("role", "lawyer", "analyst", "admin"), name="ck_users_role"),
    )

    op.create_table(
        "clients",
        _id(),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=sa.text("''")),
    )

    op.create_table(
        "matters",
        _id(),
        sa.Column("client_id", sa.BigInteger(), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'open'")),
        sa.Column("retention_days", sa.Integer(), nullable=True),
        _ts("opened_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(_in("status", "open", "on_hold", "closed"), name="ck_matters_status"),
        sa.CheckConstraint(
            "retention_days IS NULL OR retention_days > 0", name="ck_matters_retention_days"
        ),
    )
    op.create_index("ix_matters_client_id", "matters", ["client_id"])

    op.create_table(
        "touchpoints",
        _id(),
        sa.Column("matter_id", sa.BigInteger(), sa.ForeignKey("matters.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("party_role", sa.Text(), nullable=False),
        sa.Column("options", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("schedule", sa.Text(), nullable=False, server_default=sa.text("'manual'")),
        _ts("next_run_at"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.CheckConstraint(_in("kind", "page", "transcripts", "media"), name="ck_touchpoints_kind"),
        sa.CheckConstraint(
            _in("party_role", "provider", "deployer"), name="ck_touchpoints_party_role"
        ),
        sa.CheckConstraint(
            _in("schedule", "daily", "weekly", "manual"), name="ck_touchpoints_schedule"
        ),
    )
    op.create_index("ix_touchpoints_matter_id", "touchpoints", ["matter_id"])
    op.create_index(
        "ix_touchpoints_due", "touchpoints", ["next_run_at"], postgresql_where=sa.text("active")
    )

    op.create_table(
        "scans",
        _id(),
        sa.Column(
            "touchpoint_id", sa.BigInteger(), sa.ForeignKey("touchpoints.id"), nullable=False
        ),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("scanner_version", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            _in("status", "queued", "running", "done", "error"), name="ck_scans_status"
        ),
        sa.CheckConstraint(
            "status NOT IN ('done', 'error') OR finished_at IS NOT NULL",
            name="ck_scans_finished_at",
        ),
    )
    op.create_index("ix_scans_touchpoint_finished", "scans", ["touchpoint_id", "finished_at"])

    op.create_table(
        "artifacts",
        _id(),
        sa.Column("scan_id", sa.BigInteger(), sa.ForeignKey("scans.id"), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        _ts("captured_at", nullable=False),
        _ts("purged_at"),
        sa.CheckConstraint(
            _in("kind", "html", "screenshot", "transcript", "media", "report_json"),
            name="ck_artifacts_kind",
        ),
        sa.CheckConstraint(f"sha256 ~ '{SHA256_RE}'", name="ck_artifacts_sha256"),
        sa.CheckConstraint("bytes >= 0", name="ck_artifacts_bytes"),
    )
    op.create_index("ix_artifacts_scan_id", "artifacts", ["scan_id"])
    op.create_index("ix_artifacts_sha256", "artifacts", ["sha256"])

    op.create_table(
        "findings",
        _id(),
        sa.Column("scan_id", sa.BigInteger(), sa.ForeignKey("scans.id"), nullable=False),
        sa.Column("target", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("paragraph", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("location", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("basis", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.CheckConstraint(
            _in("status", "gap", "review", "pass", "info"), name="ck_findings_status"
        ),
        sa.CheckConstraint(f"fingerprint ~ '{SHA256_RE}'", name="ck_findings_fingerprint"),
    )
    op.create_index("ix_findings_scan_id", "findings", ["scan_id"])
    op.create_index("ix_findings_fingerprint", "findings", ["fingerprint"])

    op.create_table(
        "reviews",
        _id(),
        sa.Column("finding_id", sa.BigInteger(), sa.ForeignKey("findings.id"), nullable=False),
        sa.Column("reviewer_id", sa.BigInteger(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            _in(
                "decision",
                "confirm",
                "override_pass",
                "override_gap",
                "not_applicable",
                "needs_client_input",
            ),
            name="ck_reviews_decision",
        ),
        sa.CheckConstraint(
            "decision = 'confirm' OR (reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="ck_reviews_reason_required",
        ),
    )
    op.create_index("ix_reviews_finding_created", "reviews", ["finding_id", "created_at"])

    op.create_table(
        "audit_log",
        _id(),
        _ts("at", nullable=False),
        sa.Column("actor", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("prev_hash", sa.String(64), nullable=False),
        sa.Column("hash", sa.String(64), nullable=False, unique=True),
        sa.CheckConstraint(f"prev_hash ~ '{SHA256_RE}'", name="ck_audit_log_prev_hash"),
        sa.CheckConstraint(f"hash ~ '{SHA256_RE}'", name="ck_audit_log_hash"),
    )

    op.create_table(
        "webhooks",
        _id(),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.Text(), nullable=False),
        sa.Column(
            "events",
            pg.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    op.create_table(
        "deliveries",
        _id(),
        sa.Column("webhook_id", sa.BigInteger(), sa.ForeignKey("webhooks.id"), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("payload", pg.JSONB(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, server_default=sa.func.now()),
        _ts("next_attempt_at"),
        _ts("delivered_at"),
    )
    op.create_index("ix_deliveries_webhook_id", "deliveries", ["webhook_id"])
    op.create_index(
        "ix_deliveries_pending",
        "deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )


def downgrade() -> None:
    for table in (
        "deliveries",
        "webhooks",
        "audit_log",
        "reviews",
        "findings",
        "artifacts",
        "scans",
        "touchpoints",
        "matters",
        "clients",
        "users",
    ):
        op.drop_table(table)
