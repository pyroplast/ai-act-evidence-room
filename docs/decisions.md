# Design decisions

Short records of choices made where `PLAN.md` / `CLAUDE.md` left room, or where reality differed from the plan. Newest last. Each says what was decided, why, and what it costs.

## D-001 — Scanner pinned by commit SHA, not by tag (M0)

`PLAN.md` says to pin `ai-disclosure-scan` by the git tag `v0.4.0`. Upstream has no tags at all; version 0.4.0 is commit `9e72975ffb9d52eaca59e54690672ba0995335e2` ("Analyse the requested URL, and say so when it is not (0.4.0)"), whose `pyproject.toml` says `version = "0.4.0"`.

**Decision:** pin the full commit SHA in `pyproject.toml` and in `docker/fixtures-site/Dockerfile` (`SCANNER_REF`). A SHA is stricter than a tag (tags can be moved). When upstream tags `v0.5.0` in M2, switch to `@v0.5.0` and record the tag's SHA here.

**Cost:** the SHA is repeated in two files; both carry a comment pointing at each other.

**Update (M2):** upstream now has a tag. ai-disclosure-scan `v0.5.0` (commit `cefdc38c05149b6089c9c1dfb3092fe8f30566ab`, the Python API from D-016) is pinned as `@v0.5.0` in `pyproject.toml` and as `SCANNER_REF=v0.5.0` in the fixtures-site image.

## D-002 — Locked dependency versions via `constraints.txt` (M0)

`pyproject.toml` declares compatible ranges; `constraints.txt` (a `pip freeze` of a clean install with all extras) pins exact versions. Docker builds and CI install with `-c constraints.txt`, so images and CI resolve the same versions. No extra tool (uv/poetry) is required. Regenerate it with `pip freeze --exclude-editable` after a deliberate upgrade.

## D-003 — Migrations run in the app container on start (M0)

The `app` container runs `alembic upgrade head` before starting uvicorn. The worker `depends_on` the app being healthy, and `/healthz` returns 200 only when the database answers *and* `alembic_version` exists, so the worker never starts against an unmigrated schema. A separate one-shot `migrate` service would be cleaner for multi-replica app deployments; this is a single-firm, single-host deployment, so the simpler option wins. An empty `0001_baseline` revision exists so that a freshly migrated database always has an `alembic_version` row.

## D-004 — Enumerations as TEXT + CHECK, not Postgres ENUM types (M1)

Roles, statuses, kinds and review decisions are `text` columns with `CHECK (col IN (...))`. Adding a value is a one-line constraint swap in a migration instead of `ALTER TYPE ... ADD VALUE` (which cannot run inside a transaction block on older servers and cannot remove values). The allowed values are defined once in `evidence_room/models.py`.

## D-005 — SQL files are idempotent and applied by a migration that reads them (M1)

`sql/triggers.sql` and `sql/views.sql` are plain SQL kept readable for the README. Migration `0003_integrity_sql` executes them. Both files are idempotent (`CREATE OR REPLACE FUNCTION`, `DROP TRIGGER IF EXISTS` / `CREATE TRIGGER`, `DROP VIEW IF EXISTS ... CASCADE` / `CREATE VIEW`). A later change to either file must ship with a new migration that re-applies it, so a database migrated from scratch and one migrated incrementally end up identical. Downgrade drops the views and triggers.

**Cost:** replaying old migrations reads the *current* file, not the file as it was. Accepted because the files are idempotent and only ever re-applied in full.

## D-006 — Protection is stricter than the plan's minimum (M1)

- `findings`, `reviews`, `audit_log`: UPDATE and DELETE raise (as planned) **and TRUNCATE raises** (statement-level trigger). Row-level triggers alone do not fire on TRUNCATE, which would otherwise wipe the log silently.
- `artifacts`: UPDATE is allowed only when it changes `purged_at` from NULL to a timestamp and touches nothing else. Once set, `purged_at` cannot change. **DELETE and TRUNCATE raise too**, because the purge command (M5) keeps the row and its hash and only deletes the file.
- `audit_log` INSERT: a BEFORE INSERT trigger rejects a row whose `prev_hash` is not the current head's `hash` (or 64 zeros for the first row). The hash itself is computed in Python (canonical JSON is awkward in SQL); the trigger guards linkage against any writer that bypasses `audit.append`.

## D-007 — Audit chain format and concurrency (M1)

`hash = sha256(prev_hash || canonical_json(record))`, both as lowercase hex strings, where `record = {action, actor, at, entity, entity_id, payload}` serialised with sorted keys, `(",", ":")` separators, UTF-8 (`ensure_ascii=False`), `at` as ISO-8601 UTC with microseconds, `entity_id` as text. The genesis `prev_hash` is 64 zeros.

- `audit.append` takes `pg_advisory_xact_lock` on a fixed key before reading the head, so concurrent writers serialise and the chain never forks. The lock is released with the caller's transaction, so the audit row commits or rolls back with the change it records.
- Floats are rejected in payloads (they do not round-trip reliably through `jsonb`, which would make a correct chain fail verification).
- `entity_id` is text so one log can reference any entity, including ones without integer ids.
- The row `id` is not hashed; ordering is enforced by `prev_hash` linkage.

**Known limits (written down, not hidden):**
- Removing rows from the *tail* of the chain leaves a valid shorter chain. The DB already refuses DELETE; to make tail truncation detectable from outside, the evidence-pack manifest (M5) records the chain head hash at export time.
- The database role that owns the tables can `ALTER TABLE ... DISABLE TRIGGER` (the M1 tamper test does exactly that). A hardened deployment should run the app and worker as a separate non-owner role with only `SELECT`/`INSERT` on protected tables (plus the narrow `UPDATE` on `artifacts.purged_at`). Not built in v1; noted for the README Limitations section.

## D-008 — Regression and effective-status definitions in the views (M1)

- **Effective status** (`v_effective_findings`): the latest review by `(created_at, id)` wins. `confirm` → the machine status; `override_pass` → `pass`; `override_gap` → `gap`; `not_applicable` → `not_applicable`; `needs_client_input` → `needs_client_input`; no review → the machine status.
- **Regression** (`v_regressions`): *superseded by D-017.* Original definition: compares each finished scan of a touchpoint with the previous finished scan (`status = 'done'`, ordered by `finished_at, id`). A fingerprint is *regressed* when it is `gap` now and was not `gap` before (`pass`, `review`, `info` or absent). It is *resolved* when it was `gap` before and is not `gap` now (including absent). The plan says "PASS or absent"; the broader "not gap" was chosen because a `review` → `gap` move is also something a lawyer wants to hear about, and it is simpler to state. A touchpoint's first finished scan is a baseline and produces no rows. Failed scans (`status = 'error'`) are skipped, so a failure never raises a regression (M2 rule).
- Regressions use **machine** status, not effective status: a review belongs to one finding in one scan, and the alert is about what the scanner observed changing.
- **Matter summary** (`v_matter_summary`): counts by matter, Article paragraph and effective status over the latest finished scan of each *active* touchpoint.

## D-009 — `findings.target` column (M1)

`PLAN.md`'s table has no column for which scanned thing a finding belongs to. A transcripts touchpoint yields one scanner result per conversation, and a media touchpoint one per file, so `findings.target` (the scanner's `TargetResult.target`) is added. M2 decides whether `target` goes into the normalised location used for the fingerprint.

## D-010 — Tests use a real Postgres, not testcontainers (M1)

Tests need Postgres-specific behaviour (triggers, `jsonb`, advisory locks, `DISTINCT ON`). Locally `make test` starts the compose `db` service; CI uses a `postgres:16` service container. `TEST_DATABASE_URL` names the server; the suite creates and drops its own `evidence_room_test` database, so it never touches application data. One fewer dependency than testcontainers, and the same path in both places.

## D-011 — CI runs on every push and smoke-tests the full compose stack (M0/M1)

CI runs on pushes to any branch, on pull requests and on manual dispatch, so a milestone can be verified on a branch before it reaches `main`. Besides ruff and pytest against a `postgres:16` service, the `compose-smoke` job builds all three images, waits for `db`, `app` and `worker` to be up, checks `/healthz`, and launches Chromium inside the worker to prove the `browser` extra is usable. This is the check behind M0's "`docker compose up` gives a healthy app".

## D-012 — `northwind-bad` gives REVIEW, not GAP, in a static scan (M2)

PLAN.md's M2 acceptance says "`northwind-bad` produces GAP findings". The scanner (0.4.0 and 0.5.0) does not do that in static mode: it reports **PAGE-001 REVIEW** (Botpress widget, no AI wording in the page) and **LBL-001 REVIEW** (AI-made `hero.png`, no visible label). From HTML alone it cannot see what the widget says, so by design it asks a person to look. A GAP for that page (BRW-001) needs browser mode, and the page loads the real `cdn.botpress.cloud` script, which rule 6 rules out for tests and the demo. With no network the inline script draws nothing, and browser mode reports PAGE-005 (no widget).

**Decision:** keep the scanner's judgement. Changing its severities to satisfy a test would make the evidence untrue. The test asserts what the scanner really reports: `northwind-bad` gives the two REVIEW findings and no PASS; `northwind-good` gives PAGE-003 and LBL-002 PASS. The transcript and media fixtures do produce GAPs (INT-001/002/003/004, MED-001) and are tested as planned.

**Was open for M3/M6, now resolved by D-017:** switching the fixture site from good to bad changes PAGE-003 PASS / LBL-002 PASS into PAGE-001 REVIEW / LBL-001 REVIEW. These are different rule IDs, so different fingerprints, and none is a GAP. With D-008's definition, the planted page regression would raise **no** `finding.regressed` event.

## D-013 — Fingerprint = touchpoint ␟ rule ␟ subject ␟ normalised location (M2)

`sha256(touchpoint_id ␟ rule_id ␟ subject ␟ normalised_location)`, with U+001F as the separator so no two different part lists hash alike. `subject` is empty for a page touchpoint, and otherwise the file's path relative to the touchpoint's folder (plus `#conversation-id`). It has to be in the key: a transcript batch yields INT-010 at "message 1" for several conversations, and those must stay distinct. It is relative so the same folder scanned from another mount gives the same fingerprints. The location is lower-cased and whitespace-collapsed. Browser locations of the form "widget opened via …" normalise to "widget", because which launcher worked can change between runs without the page changing. Implemented in `evidence_room/fingerprint.py`, and resolves D-009.

## D-014 — Scan error semantics (M2)

A scan is all-or-nothing. If the scanner raises, reports an error for **any** target (for example HTTP 404, or one unreadable transcript file), returns no results, or reports a rule missing from its own catalogue (rule 5), the scan is stored as `error` with the message, `finished_at` is set, and `scan.failed` is audited. It gets no findings, so a partially failed scan can never make missing findings look "resolved", and the views never compare against it. Artifacts the scanner did return before failing (its `report.json` naming the error, the evidence it fetched) are kept and audited. `run_scan` returns the scan id; it does not raise for scan failures, and the failure is visible on the row. Unknown touchpoint options are refused rather than ignored.

The runner uses three short transactions (start, scan with no transaction open, record), so a long browser scan never holds locks. If recording fails after files were written to the store, those files stay as unreferenced blobs. That is harmless, because the store is content-addressed.

## D-015 — What the audit chain records per scan (M2)

`scan.started` (touchpoint, target, scanner version, who triggered it); one `artifact.stored` per artifact (kind, name, sha256, bytes); `scan.finished` (finding count by status, the artifact hashes, and `findings_digest`); or `scan.failed` (the error). `findings_digest` is one SHA-256 over every finding's fingerprint, rule, status, target, location, detail and evidence. It ties the immutable finding rows into the chain without one audit row per finding: a 100-conversation batch costs one event, not hundreds.

## D-016 — Python API upstream (ai-disclosure-scan 0.5.0); fixtures vendored for tests (M2)

The scanner had importable functions but no way to get back the content its findings were computed from, so it got a thin, additive `ai_disclosure_scan.api` module, as PLAN.md asks: [pyroplast/ai-disclosure-scan#1](https://github.com/pyroplast/ai-disclosure-scan/pull/1). `scan_pages` / `scan_transcripts` / `scan_media` return the `Report` and a list of `Evidence`: the page HTML as fetched (or the rendered DOM), every media file the label check inspected, a full-page screenshot in browser mode, and each transcript or media file exactly as read. `rule_basis(rule_id)` returns the catalogue entry. `generated_at=` makes the report JSON byte-identical for the same input. Findings are unchanged; that is tested upstream, including in browser mode. This repo stores `rule_basis()` verbatim in `findings.basis`.

`artifacts` gained `name` and `media_type` (migration 0004), so each stored file says which URL or file it came from.

The Northwind sites, transcripts and media are copied into `tests/fixtures/scanner-examples/` (see its `PROVENANCE.md`), because the scanner's wheel does not ship `examples/` and tests must not download anything. Pages are served from `127.0.0.1` by the test process. CI installs Playwright's Chromium, so the browser-mode runner test runs instead of being skipped.

## D-017 — Regressions are ranked by severity: PASS/INFO/absent → REVIEW counts (decided before M3)

Decided by the project owner after D-012 showed that switching the demo page from good to bad produces only REVIEW findings. `v_regressions` now ranks the machine status: **pass, info or absent = 0; review = 1; gap = 2**.

- **regressed:** severity went up. That covers pass/info/absent → review or gap, and review → gap.
- **resolved:** severity went down to 0. That covers review or gap → pass, info or absent.
- **gap → review** gives no row. The finding is still open and was already alerted when it became a gap.

"Absent" has to count as 0. Otherwise the planted page regression still raises nothing: the good page's PAGE-003/LBL-002 and the bad page's PAGE-001/LBL-001 are different rules, so the REVIEW findings are new fingerprints, not a status change on an old one. With this definition, good → bad at the same URL gives exactly two `regressed` rows (PAGE-001 and LBL-001, absent → review), and the PASS findings that disappeared are not reported as "resolved". That is tested end to end in `tests/test_runner.py`.

The view gains `prev_status` and `status` (with `absent` when there is no finding) for the M3 event payload. When several findings share one fingerprint in a scan, the most severe one represents it. Migration `0005_regressions_by_severity` re-applies `sql/views.sql`, per D-005. Its downgrade does not restore the old definition, because the view is derived data.

**Cost:** a brand-new REVIEW finding now raises an alert as well as a new GAP. That is deliberate (a lawyer has something new to look at), but it means more alerts than a GAP-only rule. First scans remain a silent baseline, and failed scans are still never compared (D-014).

## D-018 — Worker: claim by moving `next_run_at`, under `FOR UPDATE SKIP LOCKED` (M3)

A plain loop over `next_run_at` (no APScheduler). Each tick claims due touchpoints in **one short transaction**: `SELECT … WHERE active AND next_run_at <= now AND no scan of it is queued/running … FOR UPDATE OF t SKIP LOCKED LIMIT n`. Before committing, it moves each claimed row's `next_run_at` forward (daily +1 day, weekly +7 days, manual → NULL). After the commit the row is no longer due, so no other worker can claim it, and `SKIP LOCKED` hands a concurrent claimer other rows instead of making it wait. The scan then runs with no lock held, so a long browser scan never pins a transaction. Two tests cover this: a direct test that a second claimer receives disjoint rows, and a test where two workers tick concurrently over 10 due touchpoints and each is scanned exactly once.

- The "no scan running" condition stops a "scan now" request from starting a second scan of a touchpoint that is already being scanned.
- `next_run_at` is `claimed_at + interval`. Missed runs (the worker was down for three days) are not caught up one by one: one scan runs, and the schedule continues from then.
- **Stale scans.** A scan still `running` after `SCAN_STALE_MINUTES` (default 60) belongs to a worker that died. The reaper marks it `error` ("abandoned"), audits `scan.failed` and queues the event, which frees the touchpoint. If that worker was only slow, the runner locks the scan row before recording and leaves a row that is no longer `running` alone, so a reaped scan is never overwritten as `done`.

## D-019 — Deliveries: one row per attempt; retries 1, 5, 30, 120 minutes (M3)

PLAN.md's `deliveries` table has one row with an `attempt` counter, which would keep only the last outcome. "Every attempt logged" is taken literally instead: **each attempt is its own row** (migration 0006 adds `event_id` and `attempted_at`; `(webhook_id, event_id, attempt)` is unique). A pending row has `attempted_at` NULL and is due at `next_attempt_at`. The worker claims due rows one per transaction with `FOR UPDATE SKIP LOCKED`, sends, and records the status code or error on that row. On failure it inserts the next attempt, due **1, 5, 30, then 120 minutes after the failed attempt**. That makes five attempts in total, then it gives up (logged). Any 2xx is success. Everything else is a failure and is retried, including no response at all and 401 (a rotated secret should not lose events).

- Webhook secrets are stored in plain text, because computing an HMAC needs the raw secret. Protect the database accordingly. The secret is never written to the audit log (`webhook.created` records only the URL and events).
- Delivery attempts are not audit events: the deliveries table is their log. The event itself is audited (D-020).
- Headers: `X-Evidence-Signature: t=<unix>,v1=<hex hmac_sha256(secret, "<t>." + body)>`, `X-Evidence-Event`, `X-Evidence-Event-Id` (stable across attempts, for idempotent receivers) and `X-Evidence-Attempt`. The body is canonical JSON (sorted keys, no whitespace, UTF-8).

## D-020 — Events are queued in the scan's own transaction (outbox) (M3)

`notify.queue_events` runs inside the transaction that records a scan as `done` or `error` (and inside the reaper's transaction). It reads that scan's rows from `v_regressions` and appends one `event.emitted` audit row per event. Then it inserts one pending delivery per active, subscribed webhook (an empty `events` list means all events). A recorded scan therefore always has its events, and a rolled-back scan never does. This holds whether the scan came from the worker, `evidence-room scan`, or (in M4) the "scan now" button.

- `event_id = sha256(event ␟ scan_id ␟ fingerprint)`, stable across retries and webhooks.
- The payload has the event, matter (id, title, client), touchpoint (id, kind, target) and scan (id, previous scan, finished_at). For finding events it adds the finding: rule, paragraph, previous and current status, fingerprint, the scanner's detail, target and location, and the scanner's `basis` pinpoints with their URLs (rule 5). It also carries the evidence: the SHA-256 of the stored file the finding was computed from (page HTML, or the transcript or media file) plus the scan's `report.json` hash. Finally it has a deep link and the scope line. For a `scan.failed` event the payload carries the error.
- Deep links point at `/findings/{id}` and `/scans/{id}` under `APP_BASE_URL`. Those pages arrive with the M4 UI.

## D-021 — Email digest: one plain-text email per scan with events, off by default (M3)

Enabled only when both `SMTP_HOST` and `DIGEST_TO` are set (STARTTLS and login are optional via env). After a scan that produced events, the worker sends one email listing them: rule, Article paragraph, status change, target, evidence hash and link, ending with the scope line. It is a fixed template, with no model. Email is best effort: a failure is logged and does not affect the scan or the webhooks, which remain the system of record. It is not audited. "Digest" means one email per scan rather than one per finding, not a timed daily summary. A daily roll-up can come later if a design partner wants it.

## D-022 — The demo webhook sink verifies signatures (M3)

`docker/webhook-sink/sink.py` (standard library only) verifies `X-Evidence-Signature` with `SINK_SECRET` and a 300-second timestamp tolerance. It answers 401 to a bad signature and 200 otherwise, and records every request with `verified` and `reason`. `POST /_control {"fail_status": 500}` makes it fail on purpose, for the retry demo. Tests load the same file in-process (with the timestamp check off, because the worker signs with the tests' frozen clock), so the code the demo runs is the code the tests exercise. Its `verify()` matches `evidence_room.notify.verify()`, and a test asserts that both give the same answers.
