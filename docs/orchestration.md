# Persistent Orchestration

FitWeek implements a database-backed Run/Step state machine. It does not use an external workflow framework.

## Process model

The MySQL deployment has two deliberate process roles:

1. **FastAPI** validates requests, creates Runs, records confirmation/cancellation decisions, and reads durable state.
2. **Worker** claims ready Steps from MySQL and executes registered handlers.

The API process constructs a `MySQLOrchestrationRuntime` so it can expose services and repository-backed reads, but it does not start the runtime's Worker or Reaper. This prevents HTTP process restarts from owning or duplicating background execution.

```powershell
python -m app.orchestration.cli worker --loop --worker-id worker-01
python -m app.orchestration.cli reaper --once
```

The current CLI supplies a Worker loop and a one-shot Reaper. Continuous Reaper scheduling is an operational responsibility outside this prototype.

## Durable records

- **Run**: workflow type, user, status, request fingerprint, safe input payload, current Step, result reference, timestamps, and optimistic version.
- **Step**: type, sequence, dependencies, safe input/output, attempt budget, next execution time, worker, lease, fencing token, error state, and optimistic version.
- **Checkpoint**: one successful attempt's handler version, input fingerprint, safe output, result reference, and resulting Run/Step statuses.
- **Audit event**: ordered state transition with bounded metadata and no raw secret-bearing payload.

Dependencies are normalized in `step_dependency`. MySQL uniqueness rules cover a Run's Step sequence, a Step attempt's Checkpoint, and a Run's Audit sequence.

## Claim, lease, and fencing

`MySQLOrchestrationRepository.claim_next_step()` atomically selects an eligible Step, assigns a lease token, increments its fencing token, and marks it running. Heartbeat and completion operations require the current worker, lease token, and fence.

If a Worker disappears, the Reaper converts an expired lease into retryable work according to the retry policy. A later Worker receives a higher fence. A stale Worker cannot commit with the old fence.

## Checkpoint and resume semantics

Handlers write formal business results through application services. The orchestration repository then records Step completion, Checkpoint, successor Step, Run status, and Audit facts transactionally within the orchestration boundary.

Where business data and Step completion cannot share one transaction, handlers are designed to replay by stable request keys and result references. A retry reads the already-committed business result and completes the missing workflow bookkeeping instead of duplicating the effect.

User confirmation is represented as a durable `WAITING_USER` Step. Confirm, reject, continue, or cancel operations use expected Step IDs and idempotency fingerprints. API restarts do not erase the waiting decision point.

## Default MySQL workflow coverage

The independent MySQL Worker currently registers:

- deterministic plan generation;
- Profile Draft review/application;
- Session Design Draft application;
- Schedule Draft application;
- Calendar operation execution;
- Recovery Application.

Recovery Application uses the same typed MySQL composition in FastAPI and the independent Worker. Its Run service creates a durable first Step; the Worker registers context load, validation, action resolution, child-draft creation/review, Plan Revision creation/safety verification, confirmation, and finalization handlers. A worker replay after an Apply commit reads the idempotent Recovery result before completing missing checkpoint bookkeeping.

## Redis boundary

Run, Step, lease, fence, Checkpoint, Audit, idempotency, and result-reference facts are in MySQL. Redis may assist caches or notifications but is not required to reconstruct orchestration state. Worker correctness falls back to MySQL polling and idempotent claims.

## Safety of stored payloads

Domain constructors reject suspicious secret-bearing keys and values in Run, Step, Checkpoint, and Audit JSON. Stored workflow payloads use references and fingerprints rather than credentials, full prompts, provider response bodies, or tracebacks.

This is a defense-in-depth contract, not a substitute for normal secret management and access control.

## Verified and unverified boundaries

The test suite contains repository, API, independent Worker subprocess, Uvicorn restart, crash/reaper/reclaim, stale-writer, confirmation, cancellation, and checkpoint readback tests for implemented MySQL workflows, including Recovery Application.

The code does not provide:

- a built-in process supervisor;
- a continuous Reaper daemon mode;
- distributed tracing or metrics persistence guarantees;
- production multi-tenant authorization.
