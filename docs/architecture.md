# Architecture and Persistence Status

This document describes the current code, not an intended future platform. The audit baseline is the repository state reviewed on 2026-09-16.

## Layering

```text
HTTP contracts and dependency composition
  -> application services
  -> domain models, policies, and Repository Protocols
  -> persistence adapters
       -> InMemory adapters
       -> SQLAlchemy/MySQL adapters
```

`app/domain/` does not import SQLAlchemy. Persistence models and conversions are isolated in `app/persistence/mysql/`; the process-local adapters are in `app/persistence/memory/`.

The default `PERSISTENCE_BACKEND` is `mysql`. `app/main.py` composes MySQL-backed business services, while `app/orchestration/mysql_runtime.py` composes an independent Worker over the same durable repositories.

## Implemented capability matrix

| Capability | Code exists | MySQL adapter | Default MySQL API path | Independent MySQL Worker path |
| --- | --- | --- | --- | --- |
| Profile and constraints | Yes | Yes | Yes | Used by handlers |
| Plan revisions and Sessions | Yes | Yes | Yes | Yes |
| Check-ins and progress | Yes | Yes | Yes | Used as workflow input |
| Profile Draft review/apply | Yes | Yes | Yes | Yes |
| Session Design Draft/apply | Yes | Yes | Yes | Yes |
| Schedule Draft/apply | Yes | Yes | Yes | Yes |
| Recovery Draft | Yes | Yes | Yes | Draft creation is API-owned, not a Recovery Application workflow |
| Recovery Application result | Yes | Yes | Yes | Yes |
| Memory and Memory Candidate | Yes | Yes | Yes | Context reads use the MySQL service |
| Context Snapshot | Yes | Yes | Yes | Yes |
| Run / Step / Checkpoint / Audit | Yes | Yes | Yes | Yes |
| ICS export and Calendar operations | Yes | Yes | Yes | Calendar operation handlers are registered |
| Redis assistance | Yes | Not applicable | Optional cache/degradation path | Not a workflow fact store |

“Code exists” is not treated as runtime completion. Recovery Application is now composed from MySQL-backed repositories in both FastAPI and the independent Worker. The MySQL Worker registers its durable Run service and all Recovery Application handlers; the API never starts that Worker.

## Durable object inventory

| Object | Why it must survive restart | Current MySQL representation | Current status |
| --- | --- | --- | --- |
| Profile | Current user training facts | `fitness_profile` | Persisted and wired |
| Constraint | Hard/soft planning constraints | `user_constraint` | Persisted with Profile |
| Plan / Revision | Immutable revision history and current selection | `weekly_plan` | Persisted and wired |
| Session | Stable logical identity plus revision snapshot | `workout_session`, `session_exercise` | Persisted and wired |
| Check-in | Execution evidence used by progress and Recovery | `session_checkin` | Persisted and wired |
| Memory | Confirmed long-lived preference facts | `memory_item`, `memory_evidence` | Persisted and wired |
| Memory Candidate | Review boundary before Memory activation | `memory_candidate` | Persisted and wired |
| Context Snapshot | Exact frozen context used by an Agent/Step | `context_snapshot` | Persisted and wired |
| Agent Draft | Reviewable, reproducible model-assisted proposal | `profile_draft`, Session Design tables, Schedule tables, Recovery Draft tables | Persisted; all draft paths are wired |
| Application Result | Idempotent record of a committed draft application | Profile apply fields, `session_design_application_result`, `schedule_application_result`, `recovery_application_result` | Persisted and wired |
| Run | Workflow identity, status, request fingerprint, result reference | `planning_run` | Persisted and wired |
| Step | Claimable unit, dependency, attempts, lease and fence | `agent_step`, `step_dependency` | Persisted and wired |
| Checkpoint | Completed handler version, input fingerprint and safe output | `planning_checkpoint` | Persisted and wired |
| Audit | Ordered state-transition evidence | `audit_event` | Persisted and wired |

The schema also contains idempotency, worker lease, outbox, ICS, Calendar binding/operation, candidate-set, trace, and child-binding tables needed by the implemented slices.

## Repository mapping

Repository protocols are defined beside their domain models. Current MySQL adapters include:

- `MySQLProfileRepository`, `MySQLPlanRepository`, `MySQLCheckInRepository`, and `MySQLExerciseRepository`;
- `MySQLMemoryRepository` and `MySQLContextSnapshotRepository`;
- `MySQLProfileDraftRepository`;
- `MySQLSessionDesignRepository` and `MySQLSessionDesignApplicationRepository`;
- `MySQLScheduleDraftRepository` and `MySQLScheduleApplicationRepository`;
- `MySQLRecoveryDraftRepository` and `MySQLRecoveryApplicationRepository`;
- `MySQLOrchestrationRepository`;
- `MySQLIcsExportRepository` and `MySQLCalendarOperationRepository`.

The adapters translate between immutable domain records and SQLAlchemy rows. Application services depend on protocols rather than concrete SQLAlchemy classes.

## Migration status

Alembic revisions are linear through `0012_recovery_application_persistence`:

1. baseline domain tables;
2. core MySQL persistence;
3. Memory/Context contract completion;
4. Profile Draft;
5. Session Design Draft/application;
6. Schedule Draft/application;
7–8. durable orchestration and status capacity;
9. ICS export;
10. Calendar operations;
11. Recovery Draft artifacts;
12. Recovery Application results, child bindings, and Memory imports.

Current tests check the migration chain, upgrade/downgrade behavior, key table metadata, repository round trips, MySQL API/Worker composition, process restarts, lease reclaim, and a post-commit/pre-checkpoint Recovery replay. The schema contract comparison for the four Recovery Application tables found no semantic migration difference; no new migration was created.

## Explicit non-goals

- No Domain dependency on SQLAlchemy.
- No replacement of Repository Protocols or InMemory adapters.
- No new workflow framework, service split, queue, vector database, or event platform.
- No schema redesign unless a tested mismatch is found.
