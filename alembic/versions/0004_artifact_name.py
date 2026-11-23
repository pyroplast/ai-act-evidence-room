"""artifacts.name and artifacts.media_type: which URL or file an artifact came from.

The artifacts guard trigger compares whole rows (to_jsonb), so it covers the new
columns without changes.

Revision ID: 0004_artifact_name
Revises: 0003_integrity_sql
Create Date: 2026-11-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_artifact_name"
down_revision: str | None = "0003_integrity_sql"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "artifacts", sa.Column("name", sa.Text(), nullable=False, server_default=sa.text("''"))
    )
    op.add_column(
        "artifacts",
        sa.Column("media_type", sa.Text(), nullable=False, server_default=sa.text("''")),
    )


def downgrade() -> None:
    op.drop_column("artifacts", "media_type")
    op.drop_column("artifacts", "name")
