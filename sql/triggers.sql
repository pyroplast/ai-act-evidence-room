-- Integrity triggers. Applied by Alembic (alembic/versions/0003_integrity_sql.py).
-- Idempotent: safe to re-apply in full from a later migration (docs/decisions.md, D-005).
--
--   findings, reviews, audit_log   UPDATE, DELETE and TRUNCATE are refused.
--   artifacts                      only `purged_at` may change, once, from NULL to a time;
--                                  DELETE and TRUNCATE are refused (hashes are kept forever).
--   audit_log INSERT               prev_hash must equal the current head's hash.

-- 1. Append-only tables ------------------------------------------------------------

CREATE OR REPLACE FUNCTION evidence_refuse_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'evidence-room: % on % is not allowed (append-only table)',
        TG_OP, TG_TABLE_NAME
        USING ERRCODE = 'insufficient_privilege';
END;
$$;

DROP TRIGGER IF EXISTS findings_no_update_delete ON findings;
CREATE TRIGGER findings_no_update_delete
    BEFORE UPDATE OR DELETE ON findings
    FOR EACH ROW EXECUTE FUNCTION evidence_refuse_change();
DROP TRIGGER IF EXISTS findings_no_truncate ON findings;
CREATE TRIGGER findings_no_truncate
    BEFORE TRUNCATE ON findings
    FOR EACH STATEMENT EXECUTE FUNCTION evidence_refuse_change();

DROP TRIGGER IF EXISTS reviews_no_update_delete ON reviews;
CREATE TRIGGER reviews_no_update_delete
    BEFORE UPDATE OR DELETE ON reviews
    FOR EACH ROW EXECUTE FUNCTION evidence_refuse_change();
DROP TRIGGER IF EXISTS reviews_no_truncate ON reviews;
CREATE TRIGGER reviews_no_truncate
    BEFORE TRUNCATE ON reviews
    FOR EACH STATEMENT EXECUTE FUNCTION evidence_refuse_change();

DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log;
CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION evidence_refuse_change();
DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log;
CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION evidence_refuse_change();

-- 2. Artifacts: the only permitted change is marking a row purged ------------------

CREATE OR REPLACE FUNCTION evidence_artifacts_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP <> 'UPDATE' THEN
        RAISE EXCEPTION 'evidence-room: % on artifacts is not allowed (hashes are kept; purge sets purged_at)',
            TG_OP
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF OLD.purged_at IS NOT NULL THEN
        RAISE EXCEPTION 'evidence-room: artifact % is already purged and cannot change', OLD.id
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.purged_at IS NULL
       OR (to_jsonb(NEW) - 'purged_at') IS DISTINCT FROM (to_jsonb(OLD) - 'purged_at') THEN
        RAISE EXCEPTION 'evidence-room: UPDATE on artifacts may only set purged_at'
            USING ERRCODE = 'insufficient_privilege';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS artifacts_guard ON artifacts;
CREATE TRIGGER artifacts_guard
    BEFORE UPDATE OR DELETE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION evidence_artifacts_guard();
DROP TRIGGER IF EXISTS artifacts_no_truncate ON artifacts;
CREATE TRIGGER artifacts_no_truncate
    BEFORE TRUNCATE ON artifacts
    FOR EACH STATEMENT EXECUTE FUNCTION evidence_refuse_change();

-- 3. Audit chain linkage ---------------------------------------------------------
-- The hash itself is computed by evidence_room.audit.append (Python), which also takes
-- the advisory lock that serialises writers. This trigger only refuses a row that does
-- not link to the current head, whoever wrote it.

CREATE OR REPLACE FUNCTION evidence_audit_link_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    head text;
BEGIN
    SELECT hash INTO head FROM audit_log ORDER BY id DESC LIMIT 1;
    IF NEW.prev_hash IS DISTINCT FROM COALESCE(head, repeat('0', 64)) THEN
        RAISE EXCEPTION 'evidence-room: audit_log prev_hash % does not link to head %',
            NEW.prev_hash, COALESCE(head, repeat('0', 64))
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS audit_log_link_guard ON audit_log;
CREATE TRIGGER audit_log_link_guard
    BEFORE INSERT ON audit_log
    FOR EACH ROW EXECUTE FUNCTION evidence_audit_link_guard();
