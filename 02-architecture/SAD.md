# Software Architecture Document (SAD) — taskq-api

> Source of truth: `SPEC.md` (v1.0.0, 10 FR / 12 NFR / 12 env). Traceability: `01-requirements/SRS.md`.
> Phase 2 deliverable. Version 1.0 (Round 1).

## 1. Architecture Overview

`taskq-api` is a Python 3.11 ASGI service (FastAPI) that exposes a task-queue REST API (`/v1/*`), persists to SQLite (dev/test) or PostgreSQL (prod) through SQLAlchemy 2.x, evolves its schema through Alembic (v1 -> v2 -> v3), authenticates with hashed `X-API-Key`, authorizes by scope (`read` < `write` < `admin`), rate-limits per key with a DB-backed token bucket, runs tasks as asyncio subprocesses, and reports all failures as RFC 7807 `application/problem+json`.

Architectural style: strict 4-layer architecture enforced by `import-linter` (NFR-06).

```
              HTTP client
                  |
   +--------------v---------------+
   | api         (FastAPI routes, |   thin handlers (<= 40 lines, NFR-11)
   |  deps, middleware, handlers) |   no sqlalchemy import
   +--------------v---------------+
   | service     (business logic, |   no sqlalchemy import, no Session
   |  auth, rate limit, runner)   |
   +--------------v---------------+
   | repository  (UnitOfWork,     |   ONLY layer allowed to import sqlalchemy
   |  Session, queries)           |   (models excepted, see 2.3.5)
   +--------------v---------------+
   | models      (ORM declarative)|
   +--------------v---------------+
              SQLite / PostgreSQL

   config, errors  = independence modules (importable by all layers, import none)
```

Dependency direction is downward only. `config` and `errors` are independent leaf modules.

Package root: `03-development/src/taskq_api/`. Non-package assets at repo root: `alembic.ini`, `migrations/versions/`, `.importlinter`, `Makefile`, `.env.example`, `requirements*.txt`.

Out of scope / deferred: SRS `NFR-99` / `FR-01-deferred` (the FR-01 validation blacklist is defined by a round-1 spec absent from this repo). The architecture isolates it in a single replaceable module (`service/validation.py`), so resolving it later changes no other module.

### 1.1 System Verification Target

**Makefile target**: `verify-system` (NFR-12). Sequence, each step able to fail (no `|| true`, no leading `-`):

1. `alembic upgrade head` against a real temporary SQLite file.
2. Full test suite (`pytest 03-development/tests -q`, skipped must be 0).
3. Start the real service (`python -m taskq_api serve`, i.e. uvicorn on `taskq_api.app:app`), smoke `GET /healthz` and `GET /readyz`, then stop it.
4. `alembic downgrade base` then `alembic upgrade head` (round-trip).
5. On success print `verify-system: PASS`.

**Exercises**: high-risk modules `taskq_api.service.runner`, `taskq_api.service.auth`, `taskq_api.repository.session`, `migrations.versions.v3_split_results` run for real (real DB file, real process, real subprocess) rather than behind test doubles.

## 2. Module Design

### 2.1 Directory Structure Design Principles

Applied from the template: one responsibility per directory, subdirectories bound community size (each CRG community <= 50 nodes), each directory <= 15 source files, each file <= 400 lines (NFR-11), each function CC <= 10.

SPEC.md has no section 6 (numbering jumps 5 -> 7), so no SPEC-defined module tree exists. The tree below is derived from the layer contract in NFR-06, the required `repository/` and `service/` directories named in FR-06 / NFR-08 / NFR-11, and the high-risk module names in SPEC section 10 (`service.runner`, `service.auth`, `repository.session`). This is flagged for ADR ratification.

```
03-development/src/taskq_api/
  __init__.py
  __main__.py            CLI entry: serve / migrate / seed / healthcheck / key create
  app.py                 create_app(), lifespan (executor start, graceful drain)
  config.py              [independence] 12 TASKQ_* settings, DB-URL-masked repr
  errors.py              [independence] domain exception hierarchy (no HTTP types)
  api/
    __init__.py
    deps.py              single auth+scope dependency, request-scoped UnitOfWork
    middleware.py        correlation id, CORS, rate-limit hook
    error_handlers.py    exception -> RFC 7807 problem+json
    schemas.py           pydantic v2 request/response models
    routes_tasks.py      FR-01
    routes_runs.py       FR-02
    routes_health.py     FR-09 /healthz /readyz
    routes_metrics.py    FR-09 /v1/metrics
  service/
    __init__.py
    tasks.py             task CRUD logic, cursor pagination, 409 detection
    validation.py        FR-01 field rules (isolated deferred area)
    auth.py              key hashing, constant-time verify, scope hierarchy
    keys.py              key issue/revoke (used by CLI)
    rate_limit.py        token-bucket math + decision
    runner.py            subprocess exec, timeout, kill+wait
    executor.py          TaskGroup, semaphore, queue, drain
    health.py            liveness / readiness evaluation
    metrics.py           counters, latency percentiles
    redaction.py         NFR-04 pattern redaction
  repository/
    __init__.py
    session.py           engine, pool, session_scope (commit/rollback)
    unit_of_work.py      UnitOfWork bundling repositories (hides Session from service)
    tasks.py             task queries, selectinload, cursor paging
    results.py           task_results access
    api_keys.py          api_keys access (hash lookups only)
    rate_buckets.py      bucket row, SELECT ... FOR UPDATE
    health.py            DB ping, alembic current vs head
  models/
    __init__.py
    base.py              DeclarativeBase
    task.py              Task, Tag, task_tags
    api_key.py           ApiKey
    task_result.py       TaskResult
    rate_bucket.py       RateBucket
migrations/versions/
  v1_initial.py          tasks, api_keys, rate_buckets
  v2_tags.py             tags, task_tags, unique index on tasks.name
  v3_split_results.py    result_json -> task_results (data migration, reversible)
```

File counts per directory: root 5, api 9, service 11, repository 8, models 6, migrations/versions 3 (all <= 15). No god-module: the largest responsibility (`service/runner.py` vs `executor.py`) is split by concern (single execution vs scheduling).

### 2.2 FR to Module Mapping

| FR | Title | Modules |
|----|-------|---------|
| FR-01 | Task CRUD API | `api.routes_tasks`, `api.schemas`, `service.tasks`, `service.validation`, `repository.tasks`, `models.task` |
| FR-02 | Task run endpoint | `api.routes_runs`, `service.runner`, `service.executor`, `repository.results`, `models.task_result` |
| FR-03 | API key auth | `api.deps`, `service.auth`, `service.keys`, `repository.api_keys`, `models.api_key`, `__main__` (key create) |
| FR-04 | Scope authorization | `api.deps` (the single dependency), `service.auth` (scope hierarchy) |
| FR-05 | Rate limiting | `api.middleware`/`api.deps` (hook), `service.rate_limit`, `repository.rate_buckets`, `models.rate_bucket` |
| FR-06 | Persistence and transactions | `repository.session`, `repository.unit_of_work`, `repository.*`, `models.*` |
| FR-07 | Alembic migrations | `migrations/versions/v1_initial`, `v2_tags`, `v3_split_results`, `alembic.ini` |
| FR-08 | Async executor | `service.executor`, `service.runner`, `app` (lifespan drain) |
| FR-09 | Health and observability | `api.routes_health`, `api.routes_metrics`, `service.health`, `service.metrics`, `repository.health` |
| FR-10 | RFC 7807 errors | `api.error_handlers`, `api.middleware` (correlation id), `errors` |

Every FR maps to >= 1 module. Reverse check: each module above serves at least one FR or NFR (`config`, `service.redaction`: NFR-04 / env table).

### 2.3 Module Specifications

#### 2.3.1 api layer

| Attribute | Value |
|-----------|-------|
| Responsibility | HTTP translation only: parse, call one service function, shape response. Handlers <= 40 lines |
| External Interface | `/v1/tasks[/{id}]`, `/v1/tasks/{id}/run`, `/v1/tasks/{id}/runs`, `/v1/metrics`, `/healthz`, `/readyz`, `/openapi.json` |
| Dependencies | `service`, `repository.session`/`unit_of_work` (request-scoped UoW only), `errors`, `config` |

Logical constraints:
- Authentication, scope check and rate limit happen in ONE dependency (`api.deps`); a test asserts every `/v1` route carries it (FR-04). `/healthz`, `/readyz` are excluded explicitly (FR-03, FR-05).
- Scope check runs before any resource lookup so 403 never reveals existence (FR-04, SPEC risk R4).
- No `sqlalchemy` import (NFR-06 forbidden contract).
- Every route sets `summary` and `description` (NFR-05).

#### 2.3.2 service layer

| Attribute | Value |
|-----------|-------|
| Responsibility | Business rules, auth decisions, rate-limit math, task execution orchestration |
| External Interface | Plain functions/classes taking a `UnitOfWork`; raise `errors.*` |
| Dependencies | `repository` (via UnitOfWork and repository classes), `models`, `errors`, `config` |

Logical constraints:
- Never holds a `Session`; never imports `sqlalchemy` (FR-06, NFR-06).
- `runner`: `asyncio.create_subprocess_exec(*shlex.split(command))`, never `shell=True`; timeout via `asyncio.wait_for`; on timeout `process.kill()` then `await process.wait()` (FR-08).
- `except Exception` must never swallow `asyncio.CancelledError`; it is re-raised (NFR-03).
- `executor`: `asyncio.TaskGroup` + semaphore of `TASKQ_MAX_CONCURRENT`; bounded queue; on shutdown drain up to `TASKQ_DRAIN_TIMEOUT`, then mark `interrupted` (FR-08).
- `redaction` applied to `stdout_tail`/`stderr_tail`/logs/error bodies before persist or send (NFR-04).
- Runner persists results through its own short UoW (one transaction per state change), because it runs outside a request.

#### 2.3.3 repository layer

| Attribute | Value |
|-----------|-------|
| Responsibility | All DB access. Owns engine, pool (`pool_size=TASKQ_DB_POOL_SIZE`, `pool_pre_ping=True`), Session, transaction boundary |
| External Interface | `session_scope()` context manager, `UnitOfWork` exposing `.tasks .results .api_keys .rate_buckets .health` |
| Dependencies | `models`, `errors`, `config`, `sqlalchemy`, `alembic` (script directory read, health only) |

Logical constraints:
- One Session per request; commit on success, rollback on exception, guaranteed by context manager (FR-06, NFR-03).
- ORM or parameterized queries only; no string-built SQL (NFR-02).
- Relationship loads use explicit `selectinload`/`joinedload`; list query issues a constant number of statements (NFR-01).
- Cursor pagination (keyset on `(created_at, id)`), never OFFSET (FR-01).
- `rate_buckets` update uses `SELECT ... FOR UPDATE` inside a single transaction (FR-05). On SQLite, which ignores row locks, the write transaction (`BEGIN IMMEDIATE`) provides serialization.
- DB driver errors are translated to `errors.*` here so no driver detail reaches upper layers.

#### 2.3.4 models layer

| Attribute | Value |
|-----------|-------|
| Responsibility | SQLAlchemy 2.x declarative schema matching SPEC 5.2 at head (v3) |
| External Interface | `Task, Tag, ApiKey, TaskResult, RateBucket`, `Base.metadata` |
| Dependencies | `sqlalchemy` only |

Logical constraint: no behavior, no imports from higher layers.

#### 2.3.5 config and errors (independence)

`config`: reads 12 `TASKQ_*` env vars; the DB URL field has a masked `repr` so passwords never reach logs (NFR-04). `errors`: pure Python exception classes (`ValidationFailed`, `Unauthenticated`, `Forbidden`, `NotFound`, `Conflict`, `RateLimited`, `NotReady`) with no framework imports; `api.error_handlers` is the only place mapping them to HTTP.

Layer-contract note (decision for ADR): `models` must import `sqlalchemy` to declare the ORM, which conflicts with a literal reading of NFR-06 "no layer except repository imports sqlalchemy". Proposed `.importlinter` forbidden contract: `source_modules = taskq_api.api, taskq_api.service, taskq_api.errors, taskq_api.config`; `forbidden_modules = sqlalchemy`. `models` is the declarative schema that `repository` consumes and is excluded. Alternative (pure-domain models plus ORM mapping inside `repository`) satisfies the literal text but duplicates every entity. Status: Requires Verification with stakeholder; recorded as an open item.

### 2.4 Dependency Graph (acyclic)

```
api ------> service ------> repository ------> models
 |  \          |  \            |   \              |
 |   \         |   \           |    \             |
 +----+--------+----+----------+-----+------------+--> errors, config (leaves)
```

Allowed edges: api->service, api->repository (session/UoW only), api->errors/config, service->repository, service->models, service->errors/config, repository->models, repository->errors/config, models->(none). No edge points upward; `errors` and `config` import nothing from the package. `migrations/versions` depends only on `alembic`/`sqlalchemy` (no import from `taskq_api`), so migration history stays frozen when models change.

## 3. Interfaces and Data Flows

### 3.1 External interface summary

| Method | Path | Auth | Scope | FR |
|--------|------|------|-------|----|
| POST | /v1/tasks | key | write | FR-01 |
| GET | /v1/tasks/{id} | key | read | FR-01 |
| GET | /v1/tasks?status&limit&cursor | key | read | FR-01 |
| DELETE | /v1/tasks/{id} | key | admin | FR-01 |
| POST | /v1/tasks/{id}/run | key | write | FR-02 |
| GET | /v1/tasks/{id}/runs | key | read | FR-02 |
| GET | /v1/metrics | key | admin | FR-09 |
| GET | /healthz, /readyz | none | none | FR-09 |

### 3.2 Authenticated request flow (all `/v1/*`)

```
client -> middleware(correlation_id, CORS)
       -> deps.require_scope(S)
            1. read X-API-Key            -> missing        => 401
            2. sha256 -> repository.api_keys lookup (hmac.compare_digest)
                                         -> unknown/revoked => 401
            3. scope(key) >= S ?         -> no             => 403 (no existence info)
            4. service.rate_limit.consume (UoW, row lock) -> empty => 429 + Retry-After
       -> route handler -> service.* -> repository.* -> DB
       -> response;  any error -> error_handlers -> problem+json + X-Correlation-Id
```

Order is fixed: authenticate, authorize, rate-limit, then touch the resource.

### 3.3 Task run flow (FR-02, FR-08)

```
POST /v1/tasks/{id}/run
  -> deps (auth write, rate limit)
  -> service.tasks: load task (404), create run row status=pending      [txn 1 commit]
  -> service.executor.submit(run_id)  -> 202 {run_id}
background:
  executor (TaskGroup, semaphore N) -> runner:
     status=running                                                     [txn 2]
     create_subprocess_exec(shlex.split(cmd)) ; wait_for(timeout)
       ok      -> done/failed (exit_code)
       timeout -> kill(); await wait() -> timeout
     redaction(stdout_tail, stderr_tail)
     write task_results row, final status                               [txn 3]
shutdown: lifespan -> executor.drain(TASKQ_DRAIN_TIMEOUT) -> leftovers = interrupted
```

State machine: `pending -> running -> done | failed | timeout` (plus `interrupted` on drain expiry). `CancelledError` propagates out of every layer.

### 3.4 Readiness flow (FR-09)

```
GET /readyz -> service.health -> repository.health
     db ping fails                 -> 503 detail "database unavailable"
     alembic current != head       -> 503 detail "migration not at head"   (fail closed)
     both ok                       -> 200
```

### 3.5 Data model at head (SPEC 5.2)

`tasks(id uuid, command, name unique, status, created_at)`; `api_keys(id, key_hash, scope, created_at, revoked_at)`; `tags(id, label)`; `task_tags(task_id, tag_id)`; `task_results(id, task_id FK, exit_code, stdout_tail, stderr_tail, duration_ms, finished_at)`; `rate_buckets(key_id FK, tokens, updated_at)`. `tasks.result_json` exists v1-v2 only. Migration v3 moves its data to `task_results`; its downgrade moves it back before dropping the table.

### 3.6 Error contract (FR-10)

`errors.*` -> `api.error_handlers` -> `{type, title, status, detail, instance, correlation_id}` with `Content-Type: application/problem+json`. Mapping: 422 `/errors/validation`, 401 `/errors/unauthenticated`, 403 `/errors/forbidden`, 404 `/errors/not-found`, 409 `/errors/conflict`, 429 `/errors/rate-limited`, 503 `/errors/not-ready`, 500 `/errors/internal`. `detail` comes from a fixed whitelist of messages; the generic 500 handler never echoes exception text. A task timeout is not an HTTP error (task status `timeout`).

## 4. NFR Handling

| NFR | Dimension | Design response | Primary modules |
|-----|-----------|-----------------|-----------------|
| NFR-01 | performance | Keyset pagination, explicit eager loading, indexed `tasks.name`/`created_at`; SQL-statement counting listener in tests; p95 < 30 ms (get) / < 80 ms (list) at 10k rows via pytest-benchmark | `repository.tasks`, `repository.session` |
| NFR-02 | security | No `shell=True`/`eval`/`exec`; ORM-only SQL; SHA-256 + `hmac.compare_digest`; CORS deny-all unless `TASKQ_CORS_ORIGINS`; bandit 0 HIGH/MEDIUM; see section 6 | `service.auth`, `service.runner`, `repository.*`, `api.middleware` |
| NFR-03 | error_handling | `session_scope` commit/rollback; no bare `except`; `CancelledError` re-raised; DB failure -> `/readyz` 503, no unbounded retry; timeout kills process; migration failure rolls back | `repository.session`, `service.runner`, `service.executor` |
| NFR-04 | security | `service.redaction` regex applied to output tails/logs/error bodies; masked DB URL in `config`; plaintext key printed once by CLI, never stored | `service.redaction`, `config`, `api.error_handlers` |
| NFR-05 | documentation | Docstrings with `[FR-XX]`/`[NFR-XX]` on every public symbol; `summary`+`description` on every route | all modules, `api.routes_*` |
| NFR-06 | architecture_constraints | `.importlinter`: layers `api > service > repository > models`, independence `config`/`errors`, forbidden `sqlalchemy` (see 2.3.5 decision) ; `lint-imports` exit 0 | all layers |
| NFR-07 | license_compliance | Pinned `requirements.txt` + `requirements.lock`; allowlist MIT/BSD/Apache-2.0/PSF; SBOM at `08-config/SBOM.json` | build config |
| NFR-08 | mutation_testing | Mutation scope limited to `service/` and `repository/`; score >= 70 | `service`, `repository` |
| NFR-09 | test_assertion_quality | Zero skip/xfail; migration tests on real SQLite file with column-by-column round-trip comparison | tests, `migrations/*` |
| NFR-10 | integration_coverage | `httpx.AsyncClient(ASGITransport(app))` end-to-end; each of 401/403/404/409/422/429/503, migration round-trip, rate-limit recovery, graceful drain | `app`, `api.*` |
| NFR-11 | readability | Layered split keeps handlers thin; CC <= 10; files <= 400 lines; dirs <= 15 files | all |
| NFR-12 | execute_verification_target | `make verify-system` per section 1.1 | `Makefile`, `app`, `migrations/*` |

Latency: no network I/O beyond the DB; request path is one Session and a constant number of statements. Rate-limit adds one row-locked update per request (accepted cost for cross-worker correctness, FR-05). Cost: no external services; SQLite for dev/test, PostgreSQL for prod from the same ORM models.

Risk mapping (SPEC section 9): R1 -> `migrations.versions.v3_split_results` + real-DB round-trip test; R5 -> repository eager loading + SQL counter; R7/R8 -> `service.runner`/`executor`; R9 -> `service.health` fail-closed; R10 -> pool size + `TASKQ_MAX_CONCURRENT`; R12 -> `repository.rate_buckets` locking.

## 5. SAB Block (machine-readable, BINDING CONTRACT)

Decision records for every `-deferred` id named by SRS.md or a TEST_SPEC precondition (found by `decision_issues.resolution_ref`):

FR-01-deferred: resolved — the injection-character blacklist content is defined by no available spec; Phase 3 implements only the defined rules (non-empty, at most 1000 characters, unique name) in `taskq_api.service.validation` and invents no blacklist entries; the TEST_SPEC FR-01 case exercises only empty / oversize / empty-name inputs (ADR-011).

FR-07-deferred: resolved — `rate_buckets` is created in revision `v1_initial` as listed in SPEC 5.2 and SAD 2.1; tests assert only that the table exists at head and is gone after `downgrade base`, never which revision creates it.

<!-- SAB:START -->
```yaml
sab:
  version: "1.0"
  created_at: "2026-10-08"
  phase: 2
  project: "taskq-retry"

  layers:
    - name: api
      modules:
        - "taskq_api.api.deps"
        - "taskq_api.api.middleware"
        - "taskq_api.api.error_handlers"
        - "taskq_api.api.schemas"
        - "taskq_api.api.routes_tasks"
        - "taskq_api.api.routes_runs"
        - "taskq_api.api.routes_health"
        - "taskq_api.api.routes_metrics"
      allowed_dependencies: ["service", "repository", "independence"]
    - name: service
      modules:
        - "taskq_api.service.tasks"
        - "taskq_api.service.validation"
        - "taskq_api.service.auth"
        - "taskq_api.service.keys"
        - "taskq_api.service.rate_limit"
        - "taskq_api.service.runner"
        - "taskq_api.service.executor"
        - "taskq_api.service.health"
        - "taskq_api.service.metrics"
        - "taskq_api.service.redaction"
      allowed_dependencies: ["repository", "models", "independence"]
    - name: repository
      modules:
        - "taskq_api.repository.session"
        - "taskq_api.repository.unit_of_work"
        - "taskq_api.repository.tasks"
        - "taskq_api.repository.results"
        - "taskq_api.repository.api_keys"
        - "taskq_api.repository.rate_buckets"
        - "taskq_api.repository.health"
      allowed_dependencies: ["models", "independence"]
    - name: models
      modules:
        - "taskq_api.models.base"
        - "taskq_api.models.task"
        - "taskq_api.models.api_key"
        - "taskq_api.models.task_result"
        - "taskq_api.models.rate_bucket"
      allowed_dependencies: []
    - name: independence
      modules:
        - "taskq_api.config"
        - "taskq_api.errors"
      allowed_dependencies: []
    - name: entry
      modules:
        - "taskq_api.app"
        - "taskq_api.__main__"
      allowed_dependencies: ["api", "service", "repository", "independence"]
    - name: migrations
      modules:
        - "migrations.versions.v1_initial"
        - "migrations.versions.v2_tags"
        - "migrations.versions.v3_split_results"
      allowed_dependencies: []

  allowed_dependencies:
    - {from: api, to: service}
    - {from: api, to: repository}
    - {from: api, to: independence}
    - {from: service, to: repository}
    - {from: service, to: models}
    - {from: service, to: independence}
    - {from: repository, to: models}
    - {from: repository, to: independence}
    - {from: entry, to: api}
    - {from: entry, to: service}
    - {from: entry, to: repository}
    - {from: entry, to: independence}

  quality_targets:
    max_complexity: 10
    min_coverage: 100

  nfr_dimension_mapping: {}

  nfr_traceability:
    NFR-01:
      type: performance
      dimension: performance
      target: "p95 < 30ms (GET one) and < 80ms (list, limit=50) at 10k rows; constant SQL statement count per list request"
      module: taskq_api.repository.tasks
    NFR-02:
      type: security
      dimension: security
      target: "bandit 0 HIGH / 0 MEDIUM; 0 grep hits for shell=True, eval(, exec(; no string-built SQL"
      module: taskq_api.service.auth
    NFR-03:
      type: reliability
      dimension: error_handling
      target: "0 bare except; CancelledError always re-raised; commit on success and rollback on exception"
      module: taskq_api.repository.session
    NFR-04:
      type: security
      dimension: security
      target: "matching lines replaced by [REDACTED]; DB URL never in logs, errors or /v1/metrics"
      module: taskq_api.service.redaction
    NFR-05:
      type: documentation
      dimension: documentation
      target: "docstring coverage 100% with [FR-XX]/[NFR-XX] reference; summary and description on every endpoint"
      module: taskq_api.api.routes_tasks
    NFR-06:
      type: layering
      dimension: architecture_constraints
      target: "lint-imports exit 0 with layers api > service > repository > models, independence config/errors, sqlalchemy forbidden outside repository and models"
      module: taskq_api.app
    NFR-07:
      type: licensing
      dimension: license_compliance
      target: "all direct and transitive dependencies pinned and licensed MIT/BSD/Apache-2.0/PSF; SBOM at 08-config/SBOM.json"
      module: taskq_api.config
    NFR-08:
      type: mutation
      dimension: mutation_testing
      target: "mutation score >=70"
      module: taskq_api.service.executor
      scope_layers: ["service", "repository"]
    NFR-09:
      type: testability
      dimension: test_assertion_quality
      target: "skipped = 0; zero_assert = 0; migrations tested on a real SQLite file"
      module: migrations.versions.v3_split_results
    NFR-10:
      type: integration
      dimension: integration_coverage
      target: "integration line coverage >=80"
      module: taskq_api.app
    NFR-11:
      type: maintainability
      dimension: readability
      target: "MI >=80; CC <= 10; file <= 400 lines; directory <= 15 files; handler <= 40 lines"
      module: taskq_api.api.routes_tasks
    NFR-12:
      type: verifiability
      dimension: execute_verification_target
      target: "make verify-system exits 0 and prints verify-system: PASS"
      module: taskq_api.__main__

  advisory_only: []

  gate_score_overrides: {}

  fr_module_traceability:
    FR-01: ["taskq_api.api.routes_tasks", "taskq_api.api.schemas", "taskq_api.service.tasks", "taskq_api.service.validation", "taskq_api.repository.tasks", "taskq_api.models.task"]
    FR-02: ["taskq_api.api.routes_runs", "taskq_api.service.runner", "taskq_api.service.executor", "taskq_api.repository.results", "taskq_api.models.task_result"]
    FR-03: ["taskq_api.api.deps", "taskq_api.service.auth", "taskq_api.service.keys", "taskq_api.repository.api_keys", "taskq_api.models.api_key", "taskq_api.__main__"]
    FR-04: ["taskq_api.api.deps", "taskq_api.service.auth"]
    FR-05: ["taskq_api.api.middleware", "taskq_api.api.deps", "taskq_api.service.rate_limit", "taskq_api.repository.rate_buckets", "taskq_api.models.rate_bucket"]
    FR-06: ["taskq_api.repository.session", "taskq_api.repository.unit_of_work", "taskq_api.models.base"]
    FR-07: ["migrations.versions.v1_initial", "migrations.versions.v2_tags", "migrations.versions.v3_split_results"]
    FR-08: ["taskq_api.service.executor", "taskq_api.service.runner", "taskq_api.app"]
    FR-09: ["taskq_api.api.routes_health", "taskq_api.api.routes_metrics", "taskq_api.service.health", "taskq_api.service.metrics", "taskq_api.repository.health"]
    FR-10: ["taskq_api.api.error_handlers", "taskq_api.api.middleware", "taskq_api.errors"]

  architecture_constraints:
    - id: layers-api-service-repository-models
      executor: import-linter
      contract_type: layers
      contract_name: "Layered architecture"
      source_modules: ["taskq_api.api", "taskq_api.service", "taskq_api.repository", "taskq_api.models"]
    - id: independence-config-errors
      executor: import-linter
      contract_type: independence
      contract_name: "Config and errors are independent leaves"
      source_modules: ["taskq_api.config", "taskq_api.errors"]
    - id: forbid-sqlalchemy-outside-repository
      executor: import-linter
      contract_type: forbidden
      contract_name: "SQLAlchemy only in repository and models"
      source_modules: ["taskq_api.api", "taskq_api.service", "taskq_api.errors", "taskq_api.config", "taskq_api.app", "taskq_api.__main__"]
      forbidden_modules: ["sqlalchemy"]

  decision_issues:
    - {id: FR-01-deferred, status: resolved, blocks_phase: 3, resolution_ref: "02-architecture/SAD.md"}
    - {id: FR-07-deferred, status: resolved, blocks_phase: 3, resolution_ref: "02-architecture/SAD.md"}

  high_risk_modules:
    - "taskq_api.service.runner"
    - "taskq_api.service.auth"
    - "taskq_api.repository.session"
    - "migrations.versions.v3_split_results"

  required_artifacts:
    - {path: ".importlinter", required_by_phase: 2}
    - {path: ".env.example", required_by_phase: 3}
    - {path: "requirements.txt", required_by_phase: 3}
    - {path: "requirements.lock", required_by_phase: 3}
    - {path: "requirements-dev.txt", required_by_phase: 3}
    - {path: "alembic.ini", required_by_phase: 3}
    - {path: "Makefile", required_by_phase: 3}
    - {path: "08-config/SBOM.json", required_by_phase: 8}
```
<!-- SAB:END -->

---

## 6. Security Design (STRIDE-lite Threat Model)

Attack surface is real (public HTTP API, credentials, database, subprocess execution), so `applicability: full`. Boundaries: TB-01 client to API, TB-02 service to database, TB-03 service to OS (subprocess), TB-04 system to outbound channels (responses, logs, stored output). `owner_module` values name modules to be declared in the SAB block. `verified_by` test names are planned names, to be created from Phase 5 onward.

<!-- SEC:START -->
```yaml
security_design:
  version: "1.0"
  applicability: full   # full | none — none REQUIRES justification and skips the rest
  justification: ""     # required (>=20 chars) when applicability: none
  trust_boundaries:
    - id: TB-01
      name: "external HTTP input"
      description: "requests crossing from unauthenticated clients into the API layer"
    - id: TB-02
      name: "service to database"
      description: "business layer data crossing into the relational store via the repository layer, and migrations altering stored data"
    - id: TB-03
      name: "service to OS process"
      description: "user-supplied task command strings crossing into subprocess execution"
    - id: TB-04
      name: "system to outbound channels"
      description: "error bodies, logs and persisted task output leaving the system to clients and operators"
  threats:              # STRIDE-lite — every boundary needs >=1 threat
    - id: T-01
      boundary: TB-01
      category: spoofing
      description: "request without or with a forged X-API-Key accesses /v1 endpoints"
      mitigation: "single auth dependency; SHA-256 hash lookup; unknown or missing key returns 401"
      owner_module: "taskq_api.service.auth"
      nfr: NFR-02
      verified_by: "test_sec_t01_invalid_api_key_rejected"
    - id: T-02
      boundary: TB-01
      category: spoofing
      description: "revoked API key continues to authenticate"
      mitigation: "keys with non-null revoked_at are treated as invalid"
      owner_module: "taskq_api.service.auth"
      nfr: NFR-02
      verified_by: "test_sec_t02_revoked_key_rejected"
    - id: T-03
      boundary: TB-01
      category: spoofing
      description: "timing side channel on key comparison lets an attacker recover a hash"
      mitigation: "hmac.compare_digest constant-time comparison"
      owner_module: "taskq_api.service.auth"
      nfr: NFR-02
      verified_by: "test_sec_t03_key_compare_uses_compare_digest"
    - id: T-04
      boundary: TB-01
      category: elevation_of_privilege
      description: "read or write key performs admin action or probes resource existence through 403"
      mitigation: "scope hierarchy check in the single dependency before any resource lookup; 403 body is existence-neutral"
      owner_module: "taskq_api.api.deps"
      nfr: NFR-02
      verified_by: "test_sec_t04_insufficient_scope_403_no_existence_leak"
    - id: T-05
      boundary: TB-01
      category: denial_of_service
      description: "a client floods the API and exhausts workers and DB connections"
      mitigation: "per-key DB-backed token bucket with row lock; 429 with Retry-After"
      owner_module: "taskq_api.service.rate_limit"
      verified_by: "test_sec_t05_burst_exceeded_returns_429"
    - id: T-06
      boundary: TB-01
      category: tampering
      description: "browser from an arbitrary origin calls the API cross-origin"
      mitigation: "CORS deny-all by default; allowlist only from TASKQ_CORS_ORIGINS"
      owner_module: "taskq_api.api.middleware"
      nfr: NFR-02
      verified_by: "test_sec_t06_cors_default_denies_all_origins"
    - id: T-07
      boundary: TB-02
      category: tampering
      description: "SQL injection through task name, status filter or cursor parameter"
      mitigation: "ORM or parameterized queries only; no string-built SQL; grep gate"
      owner_module: "taskq_api.repository.tasks"
      nfr: NFR-02
      verified_by: "test_sec_t07_sql_injection_payload_treated_as_literal"
    - id: T-08
      boundary: TB-02
      category: information_disclosure
      description: "API key plaintext persisted or recoverable from the database"
      mitigation: "only 64-hex SHA-256 digest stored; plaintext printed once by CLI"
      owner_module: "taskq_api.repository.api_keys"
      nfr: NFR-04
      verified_by: "test_sec_t08_key_stored_as_sha256_only"
    - id: T-09
      boundary: TB-02
      category: tampering
      description: "v3 migration loses or corrupts result data on upgrade or downgrade"
      mitigation: "data-moving upgrade and true reverse downgrade; round-trip test on real SQLite file with column-wise comparison"
      owner_module: "migrations.versions.v3_split_results"
      nfr: NFR-03
      verified_by: "test_sec_t09_v3_roundtrip_preserves_data"
    - id: T-10
      boundary: TB-02
      category: denial_of_service
      description: "connection pool exhaustion or a stale connection stalls the service"
      mitigation: "pool_size from config, pool_pre_ping, bounded concurrency, request-scoped session closed on all paths"
      owner_module: "taskq_api.repository.session"
      nfr: NFR-03
      verified_by: "test_sec_t10_session_released_on_exception"
    - id: T-11
      boundary: TB-03
      category: tampering
      description: "shell metacharacters in a task command are interpreted by a shell (command injection)"
      mitigation: "create_subprocess_exec with shlex.split, never shell=True; bandit gate"
      owner_module: "taskq_api.service.runner"
      nfr: NFR-02
      verified_by: "test_sec_t11_shell_metacharacters_not_interpreted"
    - id: T-12
      boundary: TB-03
      category: denial_of_service
      description: "a hung task leaves an orphan process or holds a concurrency slot forever"
      mitigation: "wait_for timeout, kill() then await wait(); drain timeout marks interrupted"
      owner_module: "taskq_api.service.runner"
      nfr: NFR-03
      verified_by: "test_sec_t12_timeout_kills_subprocess_no_orphan"
    - id: T-13
      boundary: TB-03
      category: denial_of_service
      description: "unbounded task submissions spawn unlimited coroutines and processes"
      mitigation: "semaphore of TASKQ_MAX_CONCURRENT with queued overflow"
      owner_module: "taskq_api.service.executor"
      nfr: NFR-03
      verified_by: "test_sec_t13_concurrency_cap_enforced"
    - id: T-14
      boundary: TB-04
      category: information_disclosure
      description: "error body leaks stack trace, SQL, file path or schema"
      mitigation: "fixed problem+json fields; detail from whitelist; generic 500 handler"
      owner_module: "taskq_api.api.error_handlers"
      nfr: NFR-02
      verified_by: "test_sec_t14_500_body_has_no_internal_detail"
    - id: T-15
      boundary: TB-04
      category: information_disclosure
      description: "secrets (sk- tokens, Bearer, token=, DB URLs) in task output are persisted or returned"
      mitigation: "regex line redaction before persist and send"
      owner_module: "taskq_api.service.redaction"
      nfr: NFR-04
      verified_by: "test_sec_t15_secret_patterns_redacted"
    - id: T-16
      boundary: TB-04
      category: information_disclosure
      description: "database URL password appears in logs, errors or /v1/metrics"
      mitigation: "masked repr for the DB URL setting; never logged"
      owner_module: "taskq_api.config"
      nfr: NFR-04
      verified_by: "test_sec_t16_db_url_password_absent_from_logs"
    - id: T-17
      boundary: TB-04
      category: repudiation
      description: "an operator cannot link a client-visible error to server-side activity"
      mitigation: "correlation_id in body, X-Correlation-Id header and structured logs"
      owner_module: "taskq_api.api.middleware"
      nfr: NFR-02
      verified_by: "test_sec_t17_correlation_id_in_header_and_log"
```
<!-- SEC:END -->

Note: threat T-07 and T-08 reference a SQL/hash defense that also depends on the repository-only `sqlalchemy` rule (NFR-06). Residual risk: SQLite does not honor row-level locks, so the T-05 mitigation relies on write-transaction serialization in dev/test; PostgreSQL uses `FOR UPDATE`.

## 7. Error Handling

| Level | Handling Strategy |
|-------|------------------|
| Level 1 | Domain `errors.*` raised in service/repository, mapped once to problem+json (422/401/403/404/409/429/503) |
| Level 2 | Unexpected exception: rollback, log with `correlation_id`, generic 500 problem+json with no internals. `CancelledError` is never caught |
| Level 3 | Fail closed: DB down or migration behind head returns 503 on `/readyz`; no unbounded retry; drain timeout marks tasks `interrupted` |

(The template's "retry 3 times" strategy is intentionally not used: NFR-03 forbids silent unbounded DB retry and SPEC defines no retry policy.)

## 8. Technology Choices

| Technology | Rationale |
|------------|----------|
| FastAPI + pydantic v2 | ASGI, async endpoints, automatic OpenAPI for NFR-05, declarative validation (SPEC section 2) |
| SQLAlchemy 2.x | One ORM model for SQLite and PostgreSQL; explicit `Session` transactions; `selectinload` for N+1 control |
| Alembic | Reversible three-step migrations including data migration (FR-07); head check for `/readyz` |
| asyncio TaskGroup + `create_subprocess_exec` | Structured concurrency and safe subprocess execution without a shell (FR-08) |
| hashlib SHA-256 + hmac | Standard library; no extra dependency for key hashing and constant-time compare |
| import-linter | Machine-enforced layering and `sqlalchemy` ban (NFR-06) |
| httpx ASGITransport, pytest-benchmark, mutmut, bandit, pip-licenses | Verification tooling mandated by NFR-01/02/07/08/10 |
