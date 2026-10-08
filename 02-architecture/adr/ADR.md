# Architecture Decision Records (ADR) — taskq-api

> Source: `02-architecture/SAD.md` (v1.0 Round 1), `SPEC.md` v1.0.0, `01-requirements/SRS.md`.
> Runtime verified: `.venv/bin/python --version` = Python 3.11.15.
> Scope note: the SAD does not use ThreadPoolExecutor, atomic file write, or a circuit breaker; ADR-005 and ADR-009 record the SAD's actual equivalents and the rejected alternatives. Nothing here is invented beyond the SAD.

## Traceability Matrix

This traceability matrix links each ADR to the SRS requirements and the SPEC.md specification clauses it satisfies. FR/NFR titles follow `01-requirements/SRS.md`; the decision text below is the source for each mapping.

| ADR | FR | NFR | Specification basis |
|-----|----|-----|---------------------|
| ADR-001 | FR-07 | NFR-05, NFR-07 | SPEC.md stack and OpenAPI constraints; SAD section 8 |
| ADR-002 | FR-06 | NFR-06, NFR-11 | SPEC.md layering contract (C-10); SAD sections 1, 2.4 |
| ADR-003 | FR-06 | NFR-06 | SAD section 2.3.5 (open item) |
| ADR-004 | FR-06 | NFR-03, NFR-06 | SAD sections 2.3.2, 2.3.3 |
| ADR-005 | FR-02, FR-08 | NFR-02, NFR-03 | SPEC.md section 8 #25; SAD sections 2.3.2, 3.3 |
| ADR-006 | FR-03, FR-04 | NFR-02, NFR-04 | SPEC.md scope hierarchy `read` < `write` < `admin` |
| ADR-007 | FR-05 | NFR-03 | SAD section 6 note (SQLite residual risk) |
| ADR-008 | FR-01 | NFR-01 | SPEC.md list endpoint and p95 targets |
| ADR-009 | FR-10, FR-09 | NFR-03, NFR-04 | SAD section 7 (no automatic retry) |
| ADR-010 | FR-07 | NFR-03 | SRS risk R1; SAD migration design |
| ADR-011 | FR-01 | NFR-99 | SRS NFR-99 item 1 (`FR-01-deferred`) |
| ADR-012 | FR-08, FR-10 | NFR-01, NFR-02, NFR-07, NFR-08, NFR-09, NFR-10, NFR-12 | SRS NFR-12 system verification target |

Acceptance criteria for each mapped requirement stay in `01-requirements/SRS.md`; this ADR records only the architectural decision. NFR-11 is covered by ADR-002 (handler, file, directory and complexity limits). NFR-99 items other than item 1 (`interrupted` status, timeout status code, metrics percentiles, MI weighting, coverage owner, NFR-01 measurement scope) are open SRS issues with no ADR decision yet; they are not resolved here.

## ADR-001: Python 3.11 with FastAPI, pydantic v2, SQLAlchemy 2.x, Alembic

### Status
Accepted

### Context
The service is an ASGI task-queue REST API with SQLite (dev/test) and PostgreSQL (prod), reversible migrations, and generated OpenAPI documentation (NFR-05). The project venv runs Python 3.11.15. `asyncio.TaskGroup` (used in ADR-005) requires Python 3.11+. SAD section 8.

### Decision
Use Python 3.11, FastAPI + pydantic v2 (API and validation), SQLAlchemy 2.x (ORM), Alembic (migrations). Use the standard library where it is sufficient: `hashlib`/`hmac` (key hashing), `asyncio` (execution), `shlex` (command splitting).

### Rationale
One ORM model serves SQLite and PostgreSQL; FastAPI generates OpenAPI for NFR-05; Alembic provides reversible data migration (FR-07) and the head check for `/readyz`.

### Consequences
- Positive: declarative validation, automatic OpenAPI, single schema definition for both databases.
- Negative: third-party dependencies need pinning and license allowlisting (NFR-07); Python 3.11 is a hard floor.

### Alternatives Considered
- Pure standard library (http.server, sqlite3): rejected, no OpenAPI, no PostgreSQL path, manual validation.
- Flask/Django: rejected, no native async subprocess integration or automatic schema generation of the same quality.

## ADR-002: Strict four-layer architecture enforced by import-linter

### Status
Accepted

### Context
NFR-06 and NFR-11 require layering, thin handlers, and bounded module size. Layers: `api > service > repository > models`; `config` and `errors` are independence leaves (SAD 1, 2.4).

### Decision
Dependencies point downward only. `import-linter` (`.importlinter`) enforces layers, independence of `config`/`errors`, and a forbidden contract banning `sqlalchemy` from `api`, `service`, `errors`, `config`. `lint-imports` must exit 0.

### Rationale
Machine-enforced boundaries keep business logic free of persistence detail and make the service layer testable without a Session.

### Consequences
- Positive: acyclic graph, handlers <= 40 lines, files <= 400 lines, directories <= 15 files, CC <= 10.
- Negative: more modules and a UnitOfWork indirection.

### Alternatives Considered
- Flat modules: rejected, cannot enforce NFR-06.
- Hexagonal with pure-domain models: see ADR-003.

## ADR-003: `models` layer may import sqlalchemy (contract exclusion)

### Status
Proposed (Requires Verification with stakeholder; open item from SAD 2.3.5)

### Context
NFR-06 literal text says only `repository` imports `sqlalchemy`, but declarative ORM models must import it.

### Decision
The forbidden contract sources are `taskq_api.api`, `taskq_api.service`, `taskq_api.errors`, `taskq_api.config`. `models` is excluded because it is the declarative schema consumed by `repository`.

### Rationale
Avoids duplicating every entity.

### Consequences
- Positive: one definition per entity, minimal code.
- Negative: deviates from the literal NFR-06 wording; `service` still imports `models` (allowed edge service->models), so ORM classes are visible to service code.

### Alternatives Considered
- Pure-domain models plus ORM mapping inside `repository`: satisfies the literal text, duplicates every entity. Rejected pending stakeholder verification.

## ADR-004: Repository + UnitOfWork with request-scoped Session

### Status
Accepted

### Context
FR-06 and NFR-03 require guaranteed commit/rollback; the service layer must not hold a Session (SAD 2.3.2, 2.3.3).

### Decision
`repository.session.session_scope()` context manager owns engine, pool (`pool_size=TASKQ_DB_POOL_SIZE`, `pool_pre_ping=True`), commit on success, rollback on exception. `UnitOfWork` exposes `.tasks .results .api_keys .rate_buckets .health`. One Session per request; the background runner uses short UoWs, one transaction per state change. Driver errors are translated to `errors.*` in the repository.

### Rationale
Transaction boundary in one place; no driver detail leaks upward.

### Consequences
- Positive: no leaked sessions (T-10), constant statement count with explicit `selectinload`/`joinedload`.
- Negative: UoW indirection; `repository.session` is a declared high-risk module.

### Alternatives Considered
- Session injected directly into services: rejected, violates NFR-06.
- Autocommit per statement: rejected, breaks multi-step atomicity (FR-06).

## ADR-005: asyncio TaskGroup + semaphore executor with subprocess runner

### Status
Accepted

### Context
FR-02/FR-08 require running user commands with bounded concurrency, timeout, and graceful shutdown. SAD 2.3.2, 3.3. (The SAD specifies asyncio; a thread pool is not used.)

### Decision
`service.executor`: `asyncio.TaskGroup`, semaphore of `TASKQ_MAX_CONCURRENT`, bounded queue, drain up to `TASKQ_DRAIN_TIMEOUT` on lifespan shutdown, leftovers marked `interrupted`. `service.runner`: `asyncio.create_subprocess_exec(*shlex.split(command))`, never `shell=True`; `asyncio.wait_for` timeout; on timeout `process.kill()` then `await process.wait()`. `except Exception` never swallows `asyncio.CancelledError`. State machine: `pending -> running -> done | failed | timeout`, plus `interrupted`.

### Rationale
Structured concurrency, no orphan processes (T-12), no command injection (T-11), cap on coroutines (T-13). Executor (scheduling) and runner (single execution) are split to avoid a god-module.

### Consequences
- Positive: cancellation-safe, bounded resource use.
- Negative: runs in-process, so tasks are lost to `interrupted` if the drain expires; no cross-process worker pool.

### Alternatives Considered
- ThreadPoolExecutor: rejected, blocking threads add no value over native asyncio subprocess and complicate cancellation.
- External queue (Celery/RQ): rejected, adds services outside the SPEC scope.
- `shell=True`: rejected, command injection (NFR-02).

## ADR-006: SHA-256 hashed API keys, constant-time compare, scope hierarchy

### Status
Accepted

### Context
FR-03/FR-04, NFR-02, NFR-04. Scopes: `read` < `write` < `admin`.

### Decision
Store only the 64-hex SHA-256 digest (`hashlib`); verify with `hmac.compare_digest`; plaintext printed once by the CLI. A single dependency `api.deps.require_scope(S)` performs, in fixed order: authenticate, authorize, rate-limit, then resource access. `/healthz` and `/readyz` are explicit exclusions. Scope failure returns 403 before any lookup (no existence leak).

### Rationale
Standard library suffices; one dependency lets a test assert every `/v1` route carries it.

### Consequences
- Positive: threats T-01..T-04, T-08 mitigated.
- Negative: unsalted fast hash is acceptable only because keys are high-entropy random tokens (assumption: Requires Verification in key-issuing code).

### Alternatives Considered
- bcrypt/argon2: rejected, extra dependency, unnecessary for random high-entropy keys.
- Per-route auth checks: rejected, easy to miss a route.

## ADR-007: Database-backed token-bucket rate limiting

### Status
Accepted

### Context
FR-05; limits must be correct across workers.

### Decision
Per-key bucket row in `rate_buckets`, updated with `SELECT ... FOR UPDATE` in one transaction (`BEGIN IMMEDIATE` serialization on SQLite, which ignores row locks). Empty bucket gives 429 + `Retry-After`.

### Rationale
Cross-worker correctness without a new service.

### Consequences
- Positive: no external dependency.
- Negative: one row-locked write per request; on SQLite dev/test, correctness relies on write-transaction serialization (residual risk, SAD section 6 note).

### Alternatives Considered
- In-memory bucket: rejected, incorrect across workers.
- Redis: rejected, external service outside scope.

## ADR-008: Keyset (cursor) pagination on (created_at, id)

### Status
Accepted

### Context
FR-01 list endpoint; NFR-01 p95 < 80 ms at 10k rows.

### Decision
Keyset pagination, never OFFSET; indexed `tasks.name` and `created_at`; explicit eager loading; SQL-statement counter in tests.

### Rationale
Stable performance and results under concurrent inserts.

### Consequences
- Positive: constant-cost pages, no N+1.
- Negative: no random page access.

### Alternatives Considered
- OFFSET/LIMIT: rejected, degrades with depth and shifts under writes.

## ADR-009: Fail-closed error handling, RFC 7807, no automatic retry or circuit breaker

### Status
Accepted

### Context
FR-10, NFR-03, NFR-04. SAD section 7 states the template "retry 3 times" strategy is intentionally not used; the SPEC defines no retry policy and NFR-03 forbids silent unbounded DB retry. The SAD contains no circuit breaker.

### Decision
Domain `errors.*` (no framework imports) are mapped once in `api.error_handlers` to `application/problem+json` (422/401/403/404/409/429/503/500) with `correlation_id`; `detail` from a fixed whitelist; generic 500 never echoes exception text. Fail closed: DB ping failure or Alembic current != head gives 503 on `/readyz`. Secrets are redacted (`service.redaction`) before persist or send; DB URL has masked repr.

### Rationale
Predictable client contract, no information disclosure (T-14..T-17), operators can correlate errors.

### Consequences
- Positive: uniform errors, safe failure.
- Negative: transient DB faults surface as errors rather than being retried; a circuit breaker would be a new decision requiring a SPEC change.

### Alternatives Considered
- Retry with backoff: rejected (NFR-03).
- Circuit breaker: not in SAD or SPEC; not adopted.

## ADR-010: Reversible Alembic migrations v1 -> v2 -> v3 with frozen history

### Status
Accepted

### Context
FR-07; risk R1 (data loss in v3 `result_json` -> `task_results`).

### Decision
Three migrations (`v1_initial`, `v2_tags`, `v3_split_results`). v3 moves data on upgrade and reverses it on downgrade before dropping the table. Migrations import only `alembic`/`sqlalchemy`, never `taskq_api`. Verified by round-trip tests on a real SQLite file with column-by-column comparison and by `make verify-system` (upgrade, tests, serve + smoke, downgrade base, upgrade head).

### Rationale
Frozen history survives model changes; real-DB testing catches data loss.

### Consequences
- Positive: safe rollback, T-09 mitigated.
- Negative: duplicated table definitions inside migrations; `v3_split_results` is a high-risk module.

### Alternatives Considered
- Migrations importing ORM models: rejected, history drifts.
- Schema-only downgrade that drops data: rejected (R1).

## ADR-011: Isolate the deferred FR-01 validation blacklist in one module

### Status
Accepted (open item `FR-01-deferred` / `NFR-99`)

### Context
The FR-01 blacklist is defined by a round-1 spec absent from the repository.

### Decision
All FR-01 field rules live in `service/validation.py`; no other module encodes them.

### Rationale
Resolving the item later changes one module.

### Consequences
- Positive: contained change.
- Negative: FR-01 is incomplete until resolved; registered as a `decision_issue` in the SAB block.

### Alternatives Considered
- Guess the blacklist now: rejected (no fabrication).

## ADR-012: Verification tooling and system verification target

### Status
Accepted

### Context
NFR-01, 02, 07, 08, 09, 10, 12.

### Decision
`make verify-system` is the system verification target and exercises `service.runner`, `service.auth`, `repository.session`, `migrations.versions.v3_split_results` for real. Tooling: httpx `ASGITransport` end-to-end tests, pytest-benchmark, mutmut (scope `service/` and `repository/`, score >= 70), bandit (0 HIGH/MEDIUM), pip-licenses (MIT/BSD/Apache-2.0/PSF allowlist, SBOM at `08-config/SBOM.json`), zero skip/xfail.

### Rationale
Each NFR gets a machine-checkable gate.

### Consequences
- Positive: objective pass/fail.
- Negative: longer CI and more dev dependencies.

### Alternatives Considered
- Mocked-only integration tests: rejected, would not exercise high-risk modules for real (NFR-12).
