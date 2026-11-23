"""v_regressions by severity: PASS/INFO/absent -> REVIEW now counts as a regression.

Re-applies sql/views.sql in full (docs/decisions.md, D-005 and D-017). The view gains
`status` and `prev_status` columns; every earlier column keeps its meaning except
`change`, which now follows the severity ranking.

Downgrade does not restore the previous definition: views.sql holds only the current
one, and the view is derived data (no rows are lost either way). Downgrading past
0003 drops the views entirely.

Revision ID: 0005_regressions_by_severity
Revises: 0004_artifact_name
Create Date: 2026-11-23
"""

from __future__ import annotations

import importlib.util
from collections.abc import Sequence
from pathlib import Path

revision: str = "0005_regressions_by_severity"
down_revision: str | None = "0004_artifact_name"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _run_sql_file(name: str) -> None:
    # Reuse the helper from 0003 so every migration executes SQL files the same way.
    path = Path(__file__).with_name("0003_integrity_sql.py")
    spec = importlib.util.spec_from_file_location("_m0003", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.run_sql_file(name)


def upgrade() -> None:
    _run_sql_file("views.sql")


def downgrade() -> None:
    pass
