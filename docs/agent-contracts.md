# Agent Contracts

FitWeek uses four bounded agent roles. Each receives a controlled input contract and produces a draft or selection that ordinary Python code validates. Agents do not receive repository write access.

## Shared execution pattern

```text
Application service loads owned domain facts
  -> deterministic code builds Context and allowed candidates
  -> Model Gateway requests strict JSON
  -> agent-specific validator checks schema and allowed IDs
  -> deterministic fallback is available for supported failures
  -> immutable Draft is stored for review
  -> a separate application service validates and applies the Draft
```

Model output is never treated as a direct database command or arbitrary plan patch.

## Profile Agent

The Profile Agent converts one bounded user message plus controlled Profile/Context fields into a `ProfileAgentDraft`.

Implemented controls:

- deterministic pre-provider scope classification;
- closed output schema;
- controlled goal, constraint, equipment, and location values;
- request and input fingerprints;
- optional Context Snapshot reference;
- review, preview, apply, reject, expiry, idempotency, and optimistic version checks.

The Agent cannot directly update the Profile. The MySQL path persists the Draft and apply/reject result metadata in `profile_draft`; the persistent Profile workflow is registered in the independent Worker.

## Session Design Agent

The Session Designer receives one immutable template, a frozen Context Snapshot, and an `ExerciseCandidateSet` generated from the exercise catalog and user constraints.

It may select only exercise IDs already assigned to controlled slots. Validators recheck candidate membership, uniqueness, required roles, duration, equipment/location constraints, exclusions, and target Session identity. A deterministic fallback uses the same candidate set.

The Draft is reviewable and does not mutate a Plan. Application creates a new Plan revision through `SessionDesignPlanApplicationService`, followed by safety validation and confirmation. Candidate sets, Drafts, traces, and application results have MySQL tables and adapters.

## Schedule Agent

The Schedule Agent receives frozen availability windows, a busy-time snapshot, existing Session facts, Context, and a `TimeSlotCandidateSet`.

It returns candidate slot IDs rather than timestamps. Deterministic validation covers IANA timezone handling, daylight-saving transitions, `[start, end)` overlap, availability, busy intervals, duration, target week, immutable Sessions, completeness, and duplicates.

Calendar free/busy may degrade to a manual path. Creating or applying a Schedule Draft does not directly commit external Calendar events. MySQL persists busy snapshots, availability, candidate slots, Drafts, traces, and application results.

## Recovery Agent

The Recovery Agent consumes a behavior summary, change-impact snapshot, frozen Context, and a `RecoveryActionCandidateSet`. It returns only controlled action candidate IDs and unresolved Session IDs.

Deterministic code protects completed, checked-in, started, past, or otherwise immutable Sessions; verifies candidate membership, frequency, spacing, conflicts, and plan scope; and produces Memory proposals for separate review. The Agent cannot issue medical diagnoses, prescribe treatment, invent Plan patches, or directly write Memory/Calendar data.

Recovery Draft artifacts and review state are persisted and wired into the default MySQL API. Recovery Application tables and `MySQLRecoveryApplicationRepository` exist, but the application service, API dependencies, run service, and handlers are not wired into the default MySQL runtime. End-to-end persistent Recovery Application and resume are therefore missing capabilities.

## Frozen Candidate Sets

Candidate sets are immutable domain records with stable IDs, deterministic ordering, policy/catalog/context references, and a content fingerprint. They serve four purposes:

1. constrain the model's action space;
2. make validation independent from model reasoning;
3. make retries reproducible;
4. preserve exactly what was eligible when a Draft was created.

Session Design, Schedule, and Recovery each persist their candidate-set artifacts in MySQL. Profile uses controlled enums/lists and a deterministic scope boundary instead of the same candidate-set abstraction.

## Safe claims

The repository demonstrates agent contracts, validation, deterministic fallback, persistence of implemented Draft paths, and offline contract tests. It does not demonstrate clinical correctness, live-model quality, autonomous unrestricted planning, or a production multi-agent deployment.
