"""deliveries: one row per attempt, grouped by event_id.

Adds event_id (stable per event) and attempted_at; the pending-work index now keys on
attempted_at. A row records one attempt; retries are new rows (docs/decisions.md, D-019).

Revision ID: 0006_delivery_attempts
Revises: 0005_regressions_by_severity
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_delivery_attempts"
down_revision: str | None = "0005_regressions_by_severity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No deliveries exist before M3, so event_id needs no backfill beyond a placeholder.
    op.add_column(
        "deliveries", sa.Column("event_id", sa.Text(), nullable=False, server_default=sa.text("''"))
    )
    op.alter_column("deliveries", "event_id", server_default=None)
    op.add_column("deliveries", sa.Column("attempted_at", sa.DateTime(timezone=True)))
    op.alter_column("deliveries", "attempt", server_default=sa.text("1"))
    op.create_check_constraint("ck_deliveries_attempt", "deliveries", "attempt >= 1")
    op.create_unique_constraint(
        "uq_deliveries_attempt", "deliveries", ["webhook_id", "event_id", "attempt"]
    )
    op.drop_index("ix_deliveries_pending", table_name="deliveries")
    op.create_index(
        "ix_deliveries_pending",
        "deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("attempted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_deliveries_pending", table_name="deliveries")
    op.create_index(
        "ix_deliveries_pending",
        "deliveries",
        ["next_attempt_at"],
        postgresql_where=sa.text("delivered_at IS NULL"),
    )
    op.drop_constraint("uq_deliveries_attempt", "deliveries", type_="unique")
    op.drop_constraint("ck_deliveries_attempt", "deliveries", type_="check")
    op.alter_column("deliveries", "attempt", server_default=sa.text("0"))
    op.drop_column("deliveries", "attempted_at")
    op.drop_column("deliveries", "event_id")
