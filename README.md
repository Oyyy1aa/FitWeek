[English](#fitweek--persistent-multi-agent-backend-prototype) | [中文](#中文说明)

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

---

# 中文说明

## 项目概览

FitWeek 是一个后端原型：将训练目标、约束条件、可用时间和 Check-in 转换为可审阅的每周训练计划变更。它展示了如何用确定性的领域规则、持久化状态、明确的用户确认和可恢复的编排来约束模型辅助工作流。

这是一个适合工程面试展示的原型，而不是已上线的健身服务。项目刻意只使用普通 Python、FastAPI、SQLAlchemy、MySQL 和可选 Redis；不依赖 Agent 框架或分布式工作流平台。

## 为什么是 FitWeek

训练计划的变更不能只依赖模型输出：模型生成的结果必须经过业务规则、安全约束、版本控制和用户确认。FitWeek 将模型限制在可审阅的 Draft 和受控候选集内，把正式状态变更、恢复和外部副作用交给确定性的应用服务与持久化编排处理。

## 架构

```text
FastAPI API 进程
  -> 应用服务与领域校验器
  -> Profile / Session Design / Schedule / Recovery 工作流
  -> Repository Protocol
       -> SQLAlchemy/MySQL Adapter（默认持久化路径）
       -> InMemory Adapter（开发和测试）

独立编排进程
  -> Worker 从 MySQL claim 持久化 Step
  -> Handler 通过应用服务写入业务结果
  -> Checkpoint 和 Audit 记录可恢复的执行进度

可选 Redis
  -> 可重建的 Memory 缓存与辅助信号
  -> 绝不是业务或工作流状态的事实源
```

Domain 层定义不依赖 SQLAlchemy 的不可变模型和 Repository Protocol。Adapter 位于 `app/persistence/`；运行时组合位于 `app/main.py`、`app/api/dependencies.py` 和 `app/orchestration/mysql_runtime.py`。

## 四个受约束的 Agent

| Agent | 已实现职责 | 强制边界 |
| --- | --- | --- |
| Profile | 将用户消息与受控上下文转换为可审阅的 Profile Draft。 | 不能直接更新正式 Profile；Apply/reject 是独立且经过校验的操作。 |
| Session Design | 从冻结的合格集合中为单次 Session 选择 exercise ID。 | 不能虚构 exercise，也不能直接修改 Plan。 |
| Schedule | 从冻结的可用时间和 busy-time 输入中选择具备时区语义的候选时间段。 | 不能虚构时间戳，也不能写入 Calendar event。 |
| Recovery | 从行为、影响和 Context Snapshot 中选择受控的 recovery action ID。 | Draft、已审阅的 application 和 application Run 都会持久化；正式应用仍需子 Draft 审阅和 Plan 确认。 |

默认 Model provider 为 scripted/test provider。代码验证的是契约约束和回退行为，而不是在线模型的语义质量。

## Frozen Candidate Set

Session Design、Schedule 和 Recovery 不接受模型自由生成的业务决策。确定性代码会先构建有序、带 fingerprint 的 Frozen Candidate Set；模型只能返回其中的 ID，普通 Python 校验器会再次检查成员资格、完整性、冲突、不可变 Session 边界、时间安排和安全规则。

重试与 application Step 会复用已持久化的候选集，而不会静默地用新输入重新搜索。

## Memory 与 Context Snapshot

Memory 写入采用 candidate/review 生命周期；只有受控且已确认的 Memory 类型才能变为 active。Context Snapshot 保存 Agent 类型、作用域、policy 与 contract 版本、已选择内容、稳定的 Memory 引用以及 fingerprint。

默认 MySQL 路径会持久化 Memory、Memory Candidate、evidence 和 Context Snapshot。Redis 是可选且可重建的辅助层：缓存未命中、损坏或不可用时会回退到 MySQL。

## 持久化编排：Run / Step / Checkpoint

规划工作流由持久化的 Run 和有序的 Step 表示。MySQL 还保存依赖关系、lease、fencing token、Checkpoint、Audit event 和 idempotency record。

FastAPI API 只创建任务并查询工作流状态；它刻意不在进程内执行 MySQL 工作。请单独启动 Worker：

```powershell
python -m app.orchestration.cli worker --loop --worker-id local-worker-01
```

lease 过期的 Step 可以通过 Reaper 重新变为可 claim：

```powershell
python -m app.orchestration.cli reaper --once
```

默认 MySQL Worker composition 已注册 Profile、确定性 plan generation、Session Design application、Schedule application、Calendar-operation 和 Recovery Application 工作流。Recovery 在 API 与独立 Worker 中使用同一套 MySQL-backed application composition。

## 独立 Worker

独立 Worker 负责 claim 并执行持久化 Step；API 进程只负责创建 Run 和读取状态。Worker 与 one-shot Reaper 仍需由部署环境单独运行和监管，项目未提供生产级进程编排或托管部署。

## Validator 与 Safety Engine

模型输出一律被视为不可信 Draft。确定性 Validator 会执行 schema、归属、乐观版本、候选集成员资格、训练约束、时长、频率、不可变历史、时区/DST、重叠规则和明确确认边界的校验。

外部写入被隔离在 typed gateway 之后。模型调用成功不会绕过 application-service validation，也不能直接提交正式业务状态。

## Model Gateway 与 Tool Gateway

Model Gateway 提供受限的 primary/backup 尝试、超时、响应大小限制、严格 JSON 提取、schema validation、确定性 fallback、速率/并发限制和已脱敏的进程内 trace。

Tool Gateway 使用静态 registry 和 caller permission matrix，覆盖 catalog search、时长计算、Calendar free/busy、recovery spacing、ICS export、Calendar commit 和 Memory Candidate 创建。每个 descriptor 声明副作用类别、超时、重试预算、bulkhead、circuit-breaker 行为、降级策略和 idempotency 要求。

## 故障恢复与持久化

MySQL 是默认事实源。仓库包含截至 `0012_recovery_application_persistence` 的 Alembic migration、SQLAlchemy model、MySQL Adapter、InMemory Adapter 以及持久化路径的 integration test。Recovery Application 已接入默认 MySQL API 与独立 Worker 路径；其结果、子 Draft binding、Run、Step、Checkpoint 和 Audit record 可跨进程重启保留。

基于 Run / Step / Checkpoint 的恢复会在 lease 失效后允许 Step 被重新 claim，并依靠持久化业务事实和 idempotency 记录避免重复应用已提交的变更。

参见 [architecture](docs/architecture.md)、[orchestration](docs/orchestration.md) 和 [memory/context](docs/memory-context.md) 了解已验证的边界。

## Evaluation

仓库包含两个确定性的离线 evaluation dataset（200 个 plan case 与 108 个 Memory case）、schema 与 manifest 检查、安全 gate、确定性重复检查和受控 ablation。另有 Profile（50）、Session Design（60）、Schedule（72）和 Recovery（80）case 的独立契约数据集。

这些测试评估的是受控契约与确定性不变量；它们不是临床验证、用户研究、在线模型质量测量或生产负载证据。

运行离线 evaluation：

```powershell
python -m app.evaluation.runner --dataset plan
python -m app.evaluation.runner --dataset memory
python -m app.evaluation.runner --dataset ablation
```

## Quick Start

要求：Python 3.12 与可连接的 MySQL 实例；Redis 可选。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

在已忽略的 `.env` 文件中设置 `DATABASE_URL` 与 `TEST_DATABASE_URL`。可用提供的 Compose profile 启动本地一次性 MySQL：

```powershell
$env:MYSQL_CI_PASSWORD = "choose-a-local-password"
docker compose --profile mysql-ci up -d mysql-ci
```

该容器的 URL 应指向端口 `3307`、用户 `fitweek_test` 和数据库 `fitweek_test`。随后迁移并启动 API：

```powershell
alembic upgrade head
uvicorn app.main:app --reload
```

在第二个 shell 中激活相同环境并启动 Worker：

```powershell
python -m app.orchestration.cli worker --loop --worker-id local-worker-01
```

Swagger 位于 `http://127.0.0.1:8000/docs`。默认 scripted Model provider 不需要 API key。如需使用 Redis 辅助行为，运行 `docker compose up -d redis`，设置 `REDIS_ENABLED=true`，并使用本地 Redis URL。

## 当前限制

- 仅提供本地单用户模式；不包含生产级认证或授权系统。
- 不包含真实 Google Calendar OAuth 流程或公开生产部署。
- 默认 provider 展示的是契约和故障处理，而不是在线模型回答质量。
- 通用 Model Gateway trace 与进程指标是进程内数据；持久化的 agent-specific Draft 事实单独存储。
- 外部 Worker 与 one-shot Reaper 在真实部署中需要明确的进程监管。
- 前端只是部分 dashboard shell，不是完整的产品用户旅程。

## 文档

- [架构与持久化状态](docs/architecture.md)
- [编排](docs/orchestration.md)
- [Memory 与 Context](docs/memory-context.md)
- [Agent 契约](docs/agent-contracts.md)
- [工具与安全](docs/tool-safety.md)
- [可靠性边界](docs/reliability.md)
- [评估](docs/evaluation.md)
