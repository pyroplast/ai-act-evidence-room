-- Read models. Applied by Alembic (alembic/versions/0003_integrity_sql.py).
-- Idempotent: dropped and recreated in full each time (docs/decisions.md, D-005).
-- Definitions of "effective status" and "regression": docs/decisions.md, D-008.

DROP VIEW IF EXISTS v_matter_summary, v_regressions, v_effective_findings, v_latest_scan CASCADE;

-- The latest finished scan per touchpoint. Failed scans never count as "latest".
CREATE VIEW v_latest_scan AS
SELECT DISTINCT ON (s.touchpoint_id)
       s.touchpoint_id,
       s.id              AS scan_id,
       s.started_at,
       s.finished_at,
       s.scanner_version,
       s.triggered_by
FROM scans s
WHERE s.status = 'done'
ORDER BY s.touchpoint_id, s.finished_at DESC, s.id DESC;

-- Every finding with its latest review applied. The machine status is never changed;
-- the lawyer's decision sits next to it and determines effective_status.
CREATE VIEW v_effective_findings AS
SELECT f.id              AS finding_id,
       f.scan_id,
       s.touchpoint_id,
       t.matter_id,
       f.target,
       f.rule_id,
       f.paragraph,
       f.status          AS machine_status,
       f.detail,
       f.evidence,
       f.location,
       f.basis,
       f.fingerprint,
       r.id              AS review_id,
       r.decision,
       r.reason,
       r.reviewer_id,
       r.created_at      AS reviewed_at,
       CASE r.decision
           WHEN 'override_pass'      THEN 'pass'
           WHEN 'override_gap'       THEN 'gap'
           WHEN 'not_applicable'     THEN 'not_applicable'
           WHEN 'needs_client_input' THEN 'needs_client_input'
           ELSE f.status                       -- 'confirm' or no review yet
       END               AS effective_status
FROM findings f
JOIN scans s       ON s.id = f.scan_id
JOIN touchpoints t ON t.id = s.touchpoint_id
LEFT JOIN LATERAL (
    SELECT rv.*
    FROM reviews rv
    WHERE rv.finding_id = f.id
    ORDER BY rv.created_at DESC, rv.id DESC
    LIMIT 1
) r ON true;

-- Finding-by-finding diff of each finished scan against the previous finished scan of
-- the same touchpoint, matched on fingerprint, using the machine status.
--   regressed: gap now, not gap before (pass / review / info / absent)
--   resolved:  gap before, not gap now (including absent)
-- A touchpoint's first finished scan is the baseline and yields no rows.
CREATE VIEW v_regressions AS
WITH done AS (
    SELECT s.id AS scan_id,
           s.touchpoint_id,
           s.finished_at,
           lag(s.id) OVER (PARTITION BY s.touchpoint_id ORDER BY s.finished_at, s.id)
               AS prev_scan_id
    FROM scans s
    WHERE s.status = 'done'
),
pairs AS (
    SELECT * FROM done WHERE prev_scan_id IS NOT NULL
),
fp AS (
    SELECT f.scan_id,
           f.fingerprint,
           bool_or(f.status = 'gap')                   AS is_gap,
           min(f.id) FILTER (WHERE f.status = 'gap')   AS gap_finding_id,
           min(f.id)                                   AS any_finding_id,
           min(f.rule_id)                              AS rule_id,
           min(f.paragraph)                            AS paragraph
    FROM findings f
    GROUP BY f.scan_id, f.fingerprint
),
cur AS (
    SELECT p.scan_id, fp.fingerprint, fp.is_gap, fp.gap_finding_id, fp.any_finding_id,
           fp.rule_id, fp.paragraph
    FROM pairs p JOIN fp ON fp.scan_id = p.scan_id
),
prv AS (
    SELECT p.scan_id, fp.fingerprint, fp.is_gap, fp.gap_finding_id, fp.any_finding_id,
           fp.rule_id, fp.paragraph
    FROM pairs p JOIN fp ON fp.scan_id = p.prev_scan_id
),
diff AS (
    SELECT COALESCE(cur.scan_id, prv.scan_id)         AS scan_id,
           COALESCE(cur.fingerprint, prv.fingerprint) AS fingerprint,
           COALESCE(cur.rule_id, prv.rule_id)         AS rule_id,
           COALESCE(cur.paragraph, prv.paragraph)     AS paragraph,
           COALESCE(cur.is_gap, false)                AS gap_now,
           COALESCE(prv.is_gap, false)                AS gap_before,
           COALESCE(cur.gap_finding_id, cur.any_finding_id) AS finding_id,
           COALESCE(prv.gap_finding_id, prv.any_finding_id) AS prev_finding_id
    FROM cur
    FULL OUTER JOIN prv
      ON prv.scan_id = cur.scan_id AND prv.fingerprint = cur.fingerprint
)
SELECT p.touchpoint_id,
       d.scan_id,
       p.prev_scan_id,
       p.finished_at      AS scan_finished_at,
       d.fingerprint,
       d.rule_id,
       d.paragraph,
       CASE WHEN d.gap_now THEN 'regressed' ELSE 'resolved' END AS change,
       d.finding_id,
       d.prev_finding_id
FROM diff d
JOIN pairs p ON p.scan_id = d.scan_id
WHERE d.gap_now IS DISTINCT FROM d.gap_before;

-- Counts per matter, Article 50 paragraph and effective status, over the latest
-- finished scan of each active touchpoint.
CREATE VIEW v_matter_summary AS
SELECT t.matter_id,
       e.paragraph,
       e.effective_status,
       count(*)::int AS findings
FROM v_latest_scan ls
JOIN touchpoints t           ON t.id = ls.touchpoint_id AND t.active
JOIN v_effective_findings e  ON e.scan_id = ls.scan_id
GROUP BY t.matter_id, e.paragraph, e.effective_status;
