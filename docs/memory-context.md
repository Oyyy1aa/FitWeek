# Memory and Context

FitWeek separates long-lived reviewed Memory from the immutable Context Snapshot supplied to one agent workflow.

## Memory lifecycle

Supported Memory types are deliberately narrow: preferred location, preferred time of day, disliked activity, preferred equipment, and training-style preference. Medical facts are not part of the Memory enum.

A proposed item is a `MemoryCandidate` with a bounded lifetime and one of four states: pending review, accepted, rejected, or expired. Accepting a candidate creates or updates an active Memory through the repository/application-service boundary. Active Memory requires evidence and can later be superseded, expired, or deleted.

Agents do not silently promote suggestions into active Memory. Profile and Recovery flows can propose candidates, but review/import remains an explicit application operation.

## Context construction

`DeterministicContextBuilder` assembles controlled sections for an agent type:

- system policy and current task;
- Profile snapshot and hard constraints;
- soft preferences and confirmed relevant Memory;
- recent behavior summary and tool evidence;
- output contract.

Budgets cap characters, Memory entries, and behavior items. Retrieval filters by user, status, TTL, evidence, and the agent's registered contract. If Memory retrieval is unavailable, the builder can produce a declared `NO_MEMORY` degraded snapshot instead of inventing content.

## Frozen Context Snapshot

A snapshot records:

- user and agent type;
- a workflow scope ID;
- contract and policy versions;
- serialized controlled content;
- exact Memory IDs/versions represented in the content;
- character/token estimates;
- a deterministic fingerprint and creation time.

The unique `(user_id, agent_type, scope_id)` contract makes a workflow scope immutable. Concurrent identical writers converge on the stored winner; a different fingerprint for the same scope is a typed conflict.

Drafts and workflow Steps retain the snapshot reference and fingerprint. Later Memory changes therefore do not silently alter an already-created draft or resumed Step.

## Persistence and restart behavior

The default MySQL composition uses `MySQLMemoryRepository` and `MySQLContextSnapshotRepository`. It persists:

- `memory_item`;
- `memory_candidate`;
- `memory_evidence`;
- `context_snapshot`.

The API and Context service are composed from these repositories in `build_mysql_memory_services()`. Integration tests recreate application/Uvicorn processes and read the same Memory and Context Snapshot facts after restart.

## Redis is auxiliary

`RedisMemoryCache` stores a rebuildable representation of active Memory. Redis failures and malformed cache payloads are treated as cache misses. The authoritative read is MySQL, and cache writes occur after the MySQL transaction commits.

Redis does not own Memory status, evidence, candidates, snapshots, Runs, Steps, Checkpoints, or Audits. Deleting Redis data may reduce performance but must not delete business facts.

## Privacy boundary

Context is constructed from controlled fields, summaries, references, and fingerprints. Agent contracts exclude secrets, credentials, raw repository objects, and unrelated user data. Generic model traces are redacted and process-local; durable draft tables store bounded provider/context metadata rather than full prompts or raw model responses.

## Current limitations

- Retrieval is deterministic filtering/ranking, not semantic vector search.
- No cross-user or multi-tenant sharing model exists.
- Token counts are estimates based on the local budget implementation, not provider tokenizers.
- Context persistence is implemented, but exhaustive automated Domain/ORM/migration drift checking remains a follow-up verification improvement.
