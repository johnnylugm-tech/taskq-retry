# Specification Tracking Matrix — `taskq-retry`

> On-demand Lazy Load view. Not the SSOT: Status is machine-refreshed from `build_traceability` at `advance-phase`; score authority is `quality_manifest.json`. Source requirements: `SRS.md` (FR Block); canonical spec source: `SPEC.md`.

## Project Info
- Project Name: taskq-retry (taskq-api)
- Version: v1.0.0
- Created: 2026-10-08

## Specification Status

> **The Status column is machine-refreshed** — `advance-phase` overwrites each
> FR's Status from `build_traceability`'s live code/test scan (IN_PROGRESS once
> code/module exists, VERIFIED once code+test exist). The authoritative status is
> that scan / `quality_manifest.json`, NOT this hand-filled cell. Fill the
> semantic columns (Spec Description / Intent Class / Decision Framework / Notes);
> leave Status to refresh itself (a hand-edit is overwritten on the next advance).

| FR ID | Spec Description | Intent Class | Decision Framework | Status | Notes |
|-------|-----------------|--------------|-------------------|--------|-------|
| FR-01 | 任務資源 CRUD API: POST/GET/GET list/DELETE `/v1/tasks`, scopes write/read/read/admin, cursor pagination, 422/404 problem+json | CRUD | Behavioral acceptance via integration tests (httpx ASGITransport); SPEC §8 #4,#7,#8 | DRAFT | Owner: Agent A (SRS), impl P3. 注入字元黑名單 undefined — SRS NFR-99-1; source `SPEC.md` |
| FR-02 | 任務執行端點: `POST /v1/tasks/{id}/run` returns 202 + run_id, subprocess exec, state machine, `task_results`, `GET /v1/tasks/{id}/runs` | Workflow | State-machine transition tests + runner unit tests | DRAFT | Owner: Agent A (SRS), impl P3. `interrupted` state gap — NFR-99-2; timeout status code — NFR-99-3 |
| FR-03 | API Key 認證: `X-API-Key`, SHA-256 hashed storage, `hmac.compare_digest`, key create, `revoked_at`, unauthenticated health endpoints | Security | Security acceptance via integration tests; SPEC §8 #5,#18 | DRAFT | Owner: Agent A (SRS), impl P3. Plaintext key printed once only (NFR-04) |
| FR-04 | Scope 授權: read < write < admin, 403 without resource-existence leak, single dependency for all `/v1` routes | Security | Route-dependency assertion test; SPEC §8 #6 | DRAFT | Owner: Agent A (SRS), impl P3. Authorization evaluated before resource lookup |
| FR-05 | 流量控制: per-token DB-backed token bucket, 429 with `Retry-After`, row-level lock in single transaction | Control | Integration test of bucket exhaustion/refill; SPEC §8 #9 | DRAFT | Owner: Agent A (SRS), impl P3. Env: `TASKQ_RATE_BURST`, `TASKQ_RATE_PER_SEC` |
| FR-06 | 持久化層與交易邊界: repository layer, one Session per request, commit/rollback context manager, no string SQL, explicit eager loading, pool config | Data | Unit tests, `lint-imports`, SQL statement count test | DRAFT | Owner: Agent A (SRS), impl P3. Related NFR-01, NFR-02, NFR-03, NFR-06 |
| FR-07 | Schema Migration: Alembic v1/v2/v3 with working downgrade, v3 data migration reversible | Data | Real SQLite file round-trip test; SPEC §8 #12,#13 | DRAFT | Owner: Agent A (SRS), impl P3. Real DB required (NFR-09); risk R1 |
| FR-08 | 非同步執行器: TaskGroup, graceful drain, concurrency cap, `wait_for` timeout with kill and wait, CancelledError propagation | Concurrency | Integration tests incl. orphan-process check; SPEC §8 #25 | DRAFT | Owner: Agent A (SRS), impl P3. Env: `TASKQ_MAX_CONCURRENT`, `TASKQ_DRAIN_TIMEOUT`; risks R7, R8 |
| FR-09 | 健康檢查與可觀測性: `/healthz`, `/readyz` (DB and alembic head), `/v1/metrics` admin | Observability | Integration tests; SPEC §8 #10,#11 | DRAFT | Owner: Agent A (SRS), impl P3. Metrics percentile set unspecified — NFR-99-4 |
| FR-10 | 錯誤契約 RFC 7807: `application/problem+json`, fixed fields, no internal detail leak, correlation_id header and log, status mapping | Contract | Integration test per error code; SPEC §8 #19 | DRAFT | Owner: Agent A (SRS), impl P3. Related NFR-02, NFR-03; risk R6 |

## Cross-reference

- Non-functional requirements (NFR-01 to NFR-12) and deferred ambiguities (NFR-99) are tracked in `TRACEABILITY_MATRIX.md` and `SRS.md`; they are intentionally not rows above.
- Downstream deliverables: design in `02-architecture/SAD.md`, test design in `02-architecture/TEST_SPEC.md`, test plan in `04-testing/TEST_PLAN.md`.

## Update log

| Date | Change | By |
|------|--------|----|
| 2026-10-08 | Initial creation from SRS.md FR Block (FR-01 to FR-10) | Agent A |
