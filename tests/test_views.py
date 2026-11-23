"""The four read-model views in sql/views.sql behave as documented in D-008."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from tests import factories as fx


def rows(session: Session, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r._mapping) for r in session.execute(text(sql), params)]


def test_latest_scan_skips_failed_and_unfinished_scans(session: Session) -> None:
    tp = fx.touchpoint(session)
    fx.scan(session, tp, minutes=0)
    second = fx.scan(session, tp, minutes=10)
    fx.scan(session, tp, minutes=20, status="error")
    fx.scan(session, tp, minutes=30, status="running")

    latest = rows(session, "SELECT * FROM v_latest_scan WHERE touchpoint_id = :t", t=tp.id)
    assert [r["scan_id"] for r in latest] == [second.id]


def test_effective_status_follows_latest_review(session: Session) -> None:
    tp = fx.touchpoint(session)
    f = fx.finding(session, fx.scan(session, tp), status="gap")
    lawyer = fx.user(session)

    def effective() -> dict[str, Any]:
        (row,) = rows(session, "SELECT * FROM v_effective_findings WHERE finding_id = :f", f=f.id)
        return row

    assert effective()["effective_status"] == "gap"
    assert effective()["decision"] is None

    fx.review(session, f, lawyer, "override_pass", "Notice shown in widget header.", minutes=1)
    assert effective()["effective_status"] == "pass"
    assert effective()["machine_status"] == "gap"  # the machine finding never changes

    fx.review(session, f, lawyer, "needs_client_input", "Ask client about EU traffic.", minutes=2)
    assert effective()["effective_status"] == "needs_client_input"

    fx.review(session, f, lawyer, "confirm", None, minutes=3)
    row = effective()
    assert row["effective_status"] == "gap"
    assert row["decision"] == "confirm"


def test_regressions_between_consecutive_finished_scans(session: Session) -> None:
    tp = fx.touchpoint(session)

    first = fx.scan(session, tp, minutes=0)
    fx.finding(session, first, "INT-001", "pass")
    fx.finding(session, first, "INT-002", "gap")
    fx.finding(session, first, "PAG-001", "review")
    fx.finding(session, first, "MED-001", "gap")

    fx.scan(session, tp, minutes=5, status="error")  # failures never count

    second = fx.scan(session, tp, minutes=10)
    fx.finding(session, second, "INT-001", "gap")  # pass -> gap: regressed
    fx.finding(session, second, "INT-002", "pass")  # gap -> pass: resolved
    fx.finding(session, second, "PAG-001", "gap")  # review -> gap: regressed
    fx.finding(session, second, "NEW-001", "gap")  # absent -> gap: regressed
    # MED-001 absent now: resolved

    found = rows(
        session,
        "SELECT rule_id, change, prev_scan_id FROM v_regressions WHERE touchpoint_id = :t "
        "ORDER BY rule_id",
        t=tp.id,
    )
    assert [(r["rule_id"], r["change"]) for r in found] == [
        ("INT-001", "regressed"),
        ("INT-002", "resolved"),
        ("MED-001", "resolved"),
        ("NEW-001", "regressed"),
        ("PAG-001", "regressed"),
    ]
    assert {r["prev_scan_id"] for r in found} == {first.id}


def test_first_scan_is_a_baseline(session: Session) -> None:
    tp = fx.touchpoint(session)
    fx.finding(session, fx.scan(session, tp), "INT-001", "gap")
    assert rows(session, "SELECT * FROM v_regressions WHERE touchpoint_id = :t", t=tp.id) == []


def test_unchanged_scan_has_no_regressions(session: Session) -> None:
    tp = fx.touchpoint(session)
    for minutes in (0, 10):
        sc = fx.scan(session, tp, minutes=minutes)
        fx.finding(session, sc, "INT-001", "gap")
        fx.finding(session, sc, "INT-002", "pass")
    assert rows(session, "SELECT * FROM v_regressions WHERE touchpoint_id = :t", t=tp.id) == []


def test_matter_summary_counts_latest_scan_with_reviews(session: Session) -> None:
    tp = fx.touchpoint(session)
    old = fx.scan(session, tp, minutes=0)
    fx.finding(session, old, "INT-001", "gap")  # superseded by the newer scan

    new = fx.scan(session, tp, minutes=10)
    a = fx.finding(session, new, "INT-001", "gap", paragraph="50(1)")
    fx.finding(session, new, "INT-002", "gap", paragraph="50(1)")
    fx.finding(session, new, "MED-001", "pass", paragraph="50(2)")
    fx.review(session, a, fx.user(session), "override_pass", "Disclosed in the first message.")

    summary = rows(
        session,
        "SELECT paragraph, effective_status, findings FROM v_matter_summary "
        "WHERE matter_id = :m ORDER BY paragraph, effective_status",
        m=tp.matter_id,
    )
    assert summary == [
        {"paragraph": "50(1)", "effective_status": "gap", "findings": 1},
        {"paragraph": "50(1)", "effective_status": "pass", "findings": 1},
        {"paragraph": "50(2)", "effective_status": "pass", "findings": 1},
    ]
