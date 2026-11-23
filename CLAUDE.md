# ai-act-evidence-room — instructions for Claude Code

## What this repo is

A self-hosted workspace that a law firm (or an in-house legal team) runs to collect **dated, tamper-evident technical evidence** about its clients' compliance with **Article 50 of the EU AI Act** (transparency duties), lets a lawyer review every machine finding, and exports an evidence pack the lawyer can build advice on.

It sits on top of the existing scanner [`pyroplast/ai-disclosure-scan`](https://github.com/pyroplast/ai-disclosure-scan) (v0.4.0). That scanner does the checks; this repo adds matters, scheduling, evidence storage, lawyer review, regression alerts and export.

Author: Pavlo Rodionov. This is a portfolio project for Forward Deployed Engineer roles. It must read as **production-minded, deterministic and auditable**, not as an AI demo.

## Non-negotiable rules

1. **The tool never gives legal advice.** It records observable technical signals and the lawyer's decisions. It never writes conclusions like "compliant" or "non-compliant". Exported memos contain clearly marked placeholders for the lawyer's own analysis. Every report and export carries the scanner's scope line (signals only, not legal advice, not a conformity assessment).
2. **No LLM calls anywhere in the runtime.** Every result must be reproducible from the same inputs. If you think a model would help, write it in `docs/ideas.md` and don't build it.
3. **Machine findings are immutable.** Lawyer decisions are stored as separate append-only review rows on top of findings. Every review that disagrees with the machine status requires a written reason.
4. **Evidence is tamper-evident.** Every stored artifact is content-addressed by SHA-256. Every state change goes into a hash-chained audit log that Postgres itself refuses to UPDATE or DELETE (trigger). `evidence-room verify` must detect any edit to an artifact or to the audit log.
5. **Every finding cites its basis.** Keep the scanner's rule IDs and its Article/guidance references; never drop or paraphrase the `basis` text into something unverifiable.
6. **No real client data in the repo.** Demo data uses the fictional Northwind sites and transcripts from ai-disclosure-scan's `examples/`, served locally. Nothing in tests or the demo touches the public internet.
7. **Personal data hygiene.** Chat transcripts can contain personal data. Support a per-matter retention period and a purge command that deletes artifacts but keeps their hashes and a `purged` audit event.

## Stack (don't swap without writing down why in `docs/decisions.md`)

- Python 3.11, FastAPI, SQLAlchemy 2.x, Alembic, Postgres 16
- Server-rendered UI: Jinja2 + htmx. No SPA, no Node build step
- Worker: a separate container running a small scheduler loop (APScheduler or plain loop over `next_run_at`); jobs claimed with `SELECT … FOR UPDATE SKIP LOCKED`
- `ai-disclosure-scan` pinned by git tag; Playwright/Chromium only in the worker image, behind the `browser` extra
- DOCX export with `python-docx`; HTML export with Jinja
- `pytest` (+ `testcontainers` or the compose Postgres for DB tests), `ruff`
- Docker Compose: `db`, `app`, `worker`, plus a `demo` profile with `fixtures-site` (nginx serving the Northwind pages) and `webhook-sink` (tiny receiver that records deliveries)

## Engineering standards

- One command to run: `make demo` → builds, migrates, seeds, runs two scan rounds with a planted regression, and prints the URL.
- Deterministic tests: freeze time, fixed seeds, no network. Tests must pass in CI on GitHub Actions.
- Keep SQL visible: views and integrity triggers live as plain `.sql` files in `sql/`, applied by Alembic, and are shown in the README.
- README must include: the problem in two sentences, a Mermaid architecture diagram, a Mermaid ER diagram, the evidence-pack layout, a **benchmark table with real measured numbers** (from `make bench`, never invented), a Limitations section, and the scope line.
- Small commits with clear messages. Update `PLAN.md` checkboxes as milestones land.

## How to work

- Read `PLAN.md` first and do the milestones in order. Each milestone ends with its acceptance checks passing. Don't start the next one until they do.
- If the scanner lacks a clean Python API, add a thin one upstream (see M2 in `PLAN.md`) rather than shelling out to the CLI and parsing text.
- When something is ambiguous, pick the simpler option, write the choice in `docs/decisions.md`, and continue.
- Don't build: multi-tenant SaaS, billing, SSO, an LLM "summary", a crawler beyond the scanner's own `--follow-contact`, or a full DMS/Clio integration (a webhook is enough).
