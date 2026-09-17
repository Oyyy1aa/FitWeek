# Reliability Boundaries

FitWeek implements reliability controls around a local prototype. These controls are testable code contracts; they are not evidence of an operated production service.

## MySQL source of truth

The default runtime stores business facts and orchestration state in MySQL. Repository methods use explicit SQLAlchemy transactions, database uniqueness constraints, user-scoped reads, optimistic versions, and typed conflict conversion.

Important durable facts include Profiles, constraints, Plan revisions, Sessions, Check-ins, Memory, Context Snapshots, Agent Draft artifacts, implemented application results, Calendar operations, Runs, Steps, Checkpoints, and Audits.

The InMemory adapters remain useful for fast tests and local contract exploration, but they do not provide restart recovery.

## Idempotency and concurrency

Request fingerprints distinguish a safe replay from a reused request key with different content. Application-result repositories return the stored result for an identical replay and reject mismatched facts.

Optimistic versions protect mutable review state and current Plan selection. Candidate-set fingerprints and Context fingerprints protect immutable inputs. Database unique constraints provide the final concurrent-write boundary.

## Worker recovery

Steps carry leases, heartbeats, attempt counts, next execution times, and monotonic fencing tokens. A Reaper makes expired work retryable. Completion requires the current lease and fence, so an old Worker cannot overwrite a newer claim.

Checkpoints identify completed handler attempts. Stable request keys and result references allow recovery when a business transaction committed before orchestration completion was recorded.

The repository includes subprocess and restart tests for the MySQL workflows registered in the default Worker, including Recovery Application. Its crash test verifies that a committed Recovery apply is read back on lease reclaim before the missing checkpoint is completed.

## Redis degradation

Redis is disabled by default and never owns business facts. The implemented Memory cache treats connection failures and invalid payloads as misses, reads MySQL, and isolates post-commit cache-write failures.

Readiness may report Redis degradation when enabled, while liveness and MySQL facts remain independent. Redis must not be flushed or treated as a workflow reset mechanism in shared environments.

## Model and tool failure controls

- bounded attempts, deadlines, and response sizes;
- deterministic fallbacks for supported agent paths;
- static tool permissions and closed schemas;
- retry budgets, circuit breakers, and bulkheads for registered tools;
- explicit degradation for Calendar read/write and Memory Candidate creation;
- idempotency requirements for side-effecting operations.

Telemetry/exporter failures are observational and must not retry or roll back completed business operations.

## Calendar and ICS

ICS export is a separate, idempotent persisted result. Calendar operations use durable drafts, items, attempts, bindings, reconciliation, and an explicit executor workflow. External Calendar writes are disabled by default and do not occur during Schedule Draft generation.

The included adapters and tests do not establish a real provider's production behavior or OAuth correctness.

## Observability

The code contains structured logging, redaction, OpenTelemetry/Prometheus facades, Grafana dashboards, alert rules, and routing configuration. Metrics and generic traces are process-local unless an exporter is configured. The presence of dashboards and alert rules is not evidence that a monitoring stack is deployed.

## Incident response reference

These compact playbooks preserve the operational boundaries that matter to the
current code. Start with the named metrics and structured logs, recover only the
failed or retryable unit, and keep the safety and idempotency gates enabled.

### Agent success and idempotency

- **Signals:** agent success-rate alerts and idempotency-conflict counts.
- **Diagnose:** separate model, schema, validator, dependency, and final-commit failures.
- **Recovery:** retry only a retryable Step or replay the same request fingerprint.
- **Do not:** bypass validation or reuse a request key for different input.
- **Limit:** process-local metrics are lost on restart without an exporter.

### Model provider and schema failures

- **Signals:** consecutive provider failures and schema-invalid response rate.
- **Diagnose:** inspect redacted attempt metadata, timeout category, and fallback outcome.
- **Recovery:** rely on the bounded retry/fallback policy; restore the provider outside the workflow.
- **Do not:** persist an unvalidated model draft or expose raw prompts in logs.
- **Limit:** the repository does not prove production-provider availability.

### Worker backlog and expired leases

- **Signals:** oldest-waiting age, Step state, lease expiry, heartbeat, and attempt count.
- **Diagnose:** confirm that the independent Worker is running and the Reaper is being scheduled.
- **Recovery:** run the one-shot Reaper, then let a Worker claim the retryable Step with a new fence.
- **Do not:** run agent work inside the API process or let an old lease holder commit.
- **Limit:** no built-in process supervisor or Reaper scheduler is included.

### Calendar write failures and tool circuits

- **Signals:** Calendar failure rate, circuit state, deadlines, retry-budget exhaustion, and bulkhead rejection.
- **Diagnose:** inspect the durable Operation, Item, Attempt, Binding, and reconciliation records.
- **Recovery:** retry only failed or unresolved Items with the same idempotency identity.
- **Do not:** replay every successful Item or enable provider writes to repair a Draft.
- **Limit:** real provider/OAuth operations are not established by this prototype.

### Memory safety and degradation

- **Signals:** expired/deleted Memory recall counters and `NO_MEMORY` degradation rate.
- **Diagnose:** compare the Context Snapshot references with MySQL Memory state; treat Redis as disposable cache evidence only.
- **Recovery:** invalidate the auxiliary cache and read the authoritative MySQL record.
- **Do not:** restore deleted/expired facts from Redis or flush a shared Redis instance as a workflow reset.
- **Limit:** relevance and retention policies are deterministic prototype rules, not learned retrieval quality claims.

### Unauthorized tool calls

- **Signals:** permission-category Tool Gateway rejections.
- **Diagnose:** verify the caller Agent, static permission map, tool schema, and that rejection happened before adapter invocation.
- **Recovery:** correct the caller contract or permission configuration through reviewed code.
- **Do not:** disable permission gates or call an adapter directly to work around the rejection.
- **Limit:** static Agent-to-Tool authorization is not end-user authentication or RBAC.

### Observability exporter failures

- **Signals:** exporter degradation, metrics endpoint availability, and structured-log sink failures.
- **Diagnose:** distinguish sink delivery failure from completed business transactions.
- **Recovery:** restore the sink/exporter and resume new telemetry delivery.
- **Do not:** retry or roll back a completed business operation solely because telemetry failed.
- **Limit:** local dashboards and rules do not constitute a deployed monitoring service.

## Known gaps

- The Reaper CLI is one-shot and needs external scheduling.
- There is no built-in multi-process supervisor.
- Schema tests do not exhaustively compare every Domain, ORM, migration, and live-database field.
- Authentication, production authorization, load testing, backup/restore operations, and public deployment are outside the current prototype.
