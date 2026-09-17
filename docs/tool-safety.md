# Model, Tool, and Safety Boundaries

FitWeek treats model and tool calls as untrusted infrastructure behind typed boundaries. Formal business writes remain in deterministic application services and repositories.

## Model Gateway

`ModelGateway` owns provider selection and bounded execution:

- primary provider attempts are capped at two;
- backup provider use is capped at one within a maximum of three attempts;
- timeouts, response-size limits, rate limits, and concurrency limits are configurable;
- JSON extraction and Pydantic validation are mandatory;
- retryable provider failures and invalid outputs are normalized;
- agent-specific deterministic template fallback is available where configured.

The gateway records safe metadata such as provider name/version, prompt version/hash, latency, outcome, attempt count, input fingerprint, and Context reference. Its generic trace store is process-local. It does not persist full prompts, raw responses, authorization headers, API keys, or complete user messages.

## Tool Gateway

The Tool Gateway uses explicit registration; it does not discover or execute model-named functions dynamically. The registered tool set is:

| Tool | Side effect | Allowed caller boundary |
| --- | --- | --- |
| Exercise catalog search | Read only | Plan generation, Session Design, Recovery application |
| Session duration calculator | Pure | Session Design, Recovery application |
| Calendar free/busy | Read only | Schedule and Recovery application |
| Recovery spacing validator | Pure | Recovery application |
| ICS export | File write | ICS export service |
| Calendar commit | External write | Calendar executor |
| Memory Candidate create | Internal write | Memory committer |

Each descriptor declares request/response models, timeout, deadline, maximum attempts, bulkhead, circuit-breaker behavior, degradation support, and whether an idempotency key is required. The gateway checks the static caller permission matrix before invoking an adapter.

## Safety Engine and validators

The central `SafetyEngine` enforces hard planning rules without model calls or I/O. Additional validators and application services enforce:

- Profile scope confirmation and controlled constraints;
- active catalog membership, equipment/location compatibility, and exclusions;
- weekly frequency and Session duration;
- candidate membership and output completeness;
- timezone, daylight-saving, availability, busy-time, and overlap rules;
- immutable Plan history and optimistic versions;
- ownership and user isolation;
- review/confirmation requirements before formal or external writes.

Passing schema validation does not mean the model understood user intent. The design limits consequences by constraining outputs, requiring review for drafts, rejecting hard-rule violations, using explicit confirmations, and retaining versioned/audited results.

## External side effects

ICS generation and Calendar commit are separate operations. Schedule Draft creation and Plan application do not implicitly write to a provider. Calendar writes require a reviewed operation, a caller authorized as `CALENDAR_EXECUTOR`, an idempotency key, and provider-specific reconciliation/attempt tracking.

Calendar integration defaults to disabled providers. The repository includes HTTP/scripted adapters and fault/reconciliation tests, not a real OAuth grant or production provider deployment.

## Secret and payload controls

- Configuration secrets use Pydantic `SecretStr` and ignored local `.env` files.
- Run/Step/Checkpoint/Audit payload constructors reject credential-like keys and values.
- Evaluation data rejects personal and secret-bearing field names and local absolute paths.
- Logs and traces use bounded error classes and redaction rather than raw credentials or response bodies.

These controls reduce accidental leakage; they are not a replacement for production authentication, authorization, database grants, secret rotation, or network isolation.
