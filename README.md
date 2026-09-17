# FitWeek — Persistent Multi-Agent Backend Prototype

FitWeek is a backend prototype for turning training goals, constraints, availability, and check-ins into reviewable weekly-plan changes. It demonstrates how model-assisted workflows can be constrained by deterministic domain rules, durable state, explicit user confirmation, and recoverable orchestration.

This is an interview-ready engineering prototype, not a production fitness service. The repository intentionally uses ordinary Python, FastAPI, SQLAlchemy, MySQL, and optional Redis; it does not use an agent framework or distributed workflow platform.

## Architecture

```text
FastAPI API process
  -> application services and domain validators
  -> Profile / Session Design / Schedule / Recovery workflows
  -> Repository Protocols
       -> SQLAlchemy/MySQL adapters (default durable path)
       -> InMemory adapters (development and tests)

Independent orchestration process
  -> Worker claims durable Steps from MySQL
  -> handlers write business results through application services
  -> Checkpoints and Audit events record resumable progress

Optional Redis
  -> rebuildable Memory cache and auxiliary signals
  -> never the source of truth for business or workflow state
```

The Domain layer defines immutable models and repository protocols without importing SQLAlchemy. Adapters live under `app/persistence/`; runtime composition lives in `app/main.py`, `app/api/dependencies.py`, and `app/orchestration/mysql_runtime.py`.

## Four bounded agents

| Agent | Implemented responsibility | Hard boundary |
| --- | --- | --- |
| Profile | Converts a user message and controlled context into a reviewable Profile Draft. | Cannot directly update the formal Profile. Apply/reject is a separate validated operation. |
| Session Design | Selects exercise IDs for one session from a frozen eligible set. | Cannot invent exercises or directly mutate a Plan. |
| Schedule | Selects timezone-aware candidate slots from frozen availability and busy-time inputs. | Cannot invent timestamps or write Calendar events. |
| Recovery | Selects controlled recovery action IDs from behavior, impact, and context snapshots. | Drafts, reviewed applications, and application Runs are durable; application still requires explicit child-draft review and Plan confirmation. |

The default model providers are scripted/test providers. The code proves contract enforcement and fallback behavior, not live-model semantic quality.

## Frozen Candidate Set

Session Design, Schedule, and Recovery do not accept free-form model decisions. Deterministic code first builds an ordered, fingerprinted candidate set. The model may return only IDs from that set, and ordinary Python validators recheck membership, completeness, conflicts, immutable-session boundaries, timing, and safety rules.

Retries and application steps reuse the persisted candidate set instead of silently searching against newer inputs.

## Context Snapshot and Memory

Memory writes use a candidate/review lifecycle. Only controlled, confirmed Memory types can become active. A Context Snapshot stores the agent type, scope, policy and contract versions, selected content, stable Memory references, and a fingerprint.

The default MySQL path persists Memory, Memory Candidates, evidence, and Context Snapshots. Redis is optional and rebuildable: cache misses, corruption, or outages fall back to MySQL.

## Run / Step / Checkpoint orchestration

Planning workflows are represented by durable Runs and ordered Steps. MySQL also stores dependencies, leases, fencing tokens, Checkpoints, Audit events, and idempotency records.

The FastAPI process only enqueues and reads workflow state. It intentionally does not execute MySQL work in-process. Start a separate Worker:

```powershell
python -m app.orchestration.cli worker --loop --worker-id local-worker-01
```

Lease-expired Steps can be made claimable by running the Reaper:

```powershell
python -m app.orchestration.cli reaper --once
```

Profile, deterministic plan generation, Session Design application, Schedule application, Calendar-operation, and Recovery Application workflows are registered in the default MySQL Worker composition. Recovery uses the same MySQL-backed application composition in the API and independent Worker processes.

## Validator and Safety Engine

Model output is treated as an untrusted draft. Deterministic validators enforce schema, ownership, optimistic versions, candidate membership, training constraints, duration, frequency, immutable history, timezone/DST rules, overlap rules, and explicit confirmation boundaries.

External writes are isolated behind typed gateways. A successful model call never bypasses application-service validation or directly commits formal business state.

## Model Gateway and Tool Gateway

The Model Gateway provides bounded primary/backup attempts, timeouts, response-size limits, strict JSON extraction, schema validation, deterministic fallback, rate/concurrency limits, and redacted process-local traces.

The Tool Gateway uses a static registry and caller permission matrix for catalog search, duration calculation, Calendar free/busy, recovery spacing, ICS export, Calendar commit, and Memory Candidate creation. Descriptors declare side-effect class, timeout, retry budget, bulkhead, circuit-breaker behavior, degradation policy, and idempotency requirements.

## Persistence

MySQL is the default source of truth. The repository includes Alembic migrations through revision `0012_recovery_application_persistence`, SQLAlchemy models, MySQL adapters, InMemory adapters, and integration tests for the durable paths. Recovery Application is composed in the default MySQL API and independent Worker paths; its result, child bindings, Run, Step, Checkpoint, and Audit records survive process restarts.

See [architecture](docs/architecture.md), [orchestration](docs/orchestration.md), and [memory/context](docs/memory-context.md) for the verified boundaries.

## Evaluation

The repository contains two deterministic offline evaluation datasets (200 plan cases and 108 Memory cases), schema and manifest checks, safety gates, deterministic repetition checks, and controlled ablations. Separate contract datasets cover Profile (50), Session Design (60), Schedule (72), and Recovery (80) cases.

These tests evaluate controlled contracts and deterministic invariants. They are not clinical validation, user research, live-model quality measurement, or production load evidence.

Run the offline evaluation:

```powershell
python -m app.evaluation.runner --dataset plan
python -m app.evaluation.runner --dataset memory
python -m app.evaluation.runner --dataset ablation
```

## Quick Start

Requirements: Python 3.12 and a reachable MySQL instance. Redis is optional.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Set `DATABASE_URL` and `TEST_DATABASE_URL` in the ignored `.env` file. A local disposable MySQL can be started with the provided Compose profile:

```powershell
$env:MYSQL_CI_PASSWORD = "choose-a-local-password"
docker compose --profile mysql-ci up -d mysql-ci
```

For that container, point the URLs at port `3307`, user `fitweek_test`, and database `fitweek_test`. Then migrate and start the API:

```powershell
alembic upgrade head
uvicorn app.main:app --reload
```

In a second shell, activate the same environment and start the Worker:

```powershell
python -m app.orchestration.cli worker --loop --worker-id local-worker-01
```

Swagger is available at `http://127.0.0.1:8000/docs`. The default scripted model provider requires no API key. To exercise Redis-assisted behavior, start `docker compose up -d redis`, set `REDIS_ENABLED=true`, and use a local Redis URL.

## Current limitations

- Local single-user mode only; there is no production authentication or authorization system.
- No real Google Calendar OAuth flow or public deployment is included.
- Default providers demonstrate contracts and failure handling, not live-model answer quality.
- Generic Model Gateway traces and process metrics are process-local; durable agent-specific draft facts are stored separately.
- The external Worker and one-shot Reaper require explicit process supervision in a real deployment.
- The frontend is a partial dashboard shell, not a complete product journey.

## Documentation

- [Architecture and persistence status](docs/architecture.md)
- [Orchestration](docs/orchestration.md)
- [Memory and Context](docs/memory-context.md)
- [Agent contracts](docs/agent-contracts.md)
- [Tooling and safety](docs/tool-safety.md)
- [Reliability boundaries](docs/reliability.md)
- [Evaluation](docs/evaluation.md)
