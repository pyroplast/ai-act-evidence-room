# ai-act-evidence-room

Law firms advising on Article 50 of the EU AI Act need to show what a client's chatbot, pages and AI-generated media actually did on a given date, and keep that evidence intact. This self-hosted workspace runs [`ai-disclosure-scan`](https://github.com/pyroplast/ai-disclosure-scan) on a schedule per client matter, stores hashed, tamper-evident evidence, lets a lawyer confirm or override every machine finding with reasons, and exports a dated evidence pack.

> **Scope:** technical signals only, recorded by an automated scanner and reviewed by a lawyer. Not legal advice and not a conformity assessment under the EU AI Act.

**Status:** under construction. See [`PLAN.md`](PLAN.md) for milestones and [`docs/decisions.md`](docs/decisions.md) for design choices. The full README (architecture, ER diagram, evidence-pack layout, measured benchmarks, limitations) lands with milestone M6.

## Quick start (development)

Requirements: Docker with Compose v2, GNU Make, Python 3.11 for running tests on the host.

```bash
make up      # db + app + worker, waits until healthy
curl localhost:8000/healthz
make test    # pytest against the compose Postgres
make lint    # ruff
```

No LLM is called anywhere at runtime; every result is reproducible from the same inputs.
