# TEST_PLAN — taskq-api (Phase 4)

Source: `01-requirements/SRS.md` (10 FR / 12 NFR + NFR-99), `.methodology/quality_manifest.json`.
Scope: all FR-01..FR-10, NFR-01..NFR-12, NFR-99. Test IDs: `TC-FRxx-nn` / `TC-NFRxx-nn`.
Category: P=positive, N=negative, B=boundary, E=edge. Priority: P0 (blocking) / P1 / P2.
Rules: no skip/xfail, every test has an assert (NFR-09); integration tests drive `httpx.AsyncClient(transport=ASGITransport(app))` (AC-N10.2); migrations use a real SQLite file (AC-N9.5). JS-style naming does not apply; pytest names follow `test_frNN_<desc>`.

## 1. Functional requirement test cases

### FR-01 Task CRUD (AC-1.1..1.9)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR01-01 | P | Create task | POST /v1/tasks, write key, valid TaskCreate | 201, body has task id | P0 | 1.1 |
| TC-FR01-02 | P | Get one task | GET /v1/tasks/{id}, read key | 200, all fields present | P0 | 1.2 |
| TC-FR01-03 | P | List with status filter | GET /v1/tasks?status=pending | 200, only pending tasks | P0 | 1.3 |
| TC-FR01-04 | P | Delete task with results in one transaction | DELETE /v1/tasks/{id}, admin key, task has task_results rows | 204/200; task and results rows gone | P0 | 1.4 |
| TC-FR01-05 | P | Cursor pagination walk | 120 tasks, limit=50, follow cursor | 3 pages, no dup/miss, stable order | P0 | 1.7 |
| TC-FR01-06 | N | Empty command | command="" | 422 problem+json type /errors/validation | P0 | 1.5 |
| TC-FR01-07 | N | Command >1000 chars | 1001-char command | 422 problem+json | P0 | 1.5 |
| TC-FR01-08 | N | Injection-char command | command with blacklisted chars (set per NFR-99-1 resolution) | 422 problem+json | P1 | 1.5 |
| TC-FR01-09 | N | Unknown id on GET/DELETE | random uuid | 404 problem+json /errors/not-found | P0 | 1.6 |
| TC-FR01-10 | N | Duplicate name | POST same name twice | second 409 /errors/conflict | P0 | 1.9 |
| TC-FR01-11 | N | limit above max | ?limit=201 | 422 | P0 | 1.8 |
| TC-FR01-12 | N | offset param | ?offset=10 | not honored; pagination remains cursor-based (no offset in OpenAPI) | P1 | 1.7 |
| TC-FR01-13 | N | Invalid cursor | ?cursor=garbage | 422 problem+json | P1 | 1.3 |
| TC-FR01-14 | N | Invalid status value | ?status=bogus | 422 | P2 | 1.3 |
| TC-FR01-15 | B | command length 1000 / 1 | exactly 1000 chars; 1 char | 201 | P0 | 1.5 |
| TC-FR01-16 | B | limit 1 / 50 default / 200 | boundary values; omitted limit | 200; omitted returns <=50; 200 accepted | P0 | 1.8 |
| TC-FR01-17 | B | limit=0 and negative | ?limit=0, ?limit=-1 | 422 | P1 | 1.8 |
| TC-FR01-18 | E | Empty list | no tasks | 200, empty items, no next cursor | P1 | 1.3 |
| TC-FR01-19 | E | Name uniqueness after delete | delete then re-create same name | 201 | P1 | 1.9 |
| TC-FR01-20 | E | Unicode / whitespace-only name and command | "  ", CJK name | whitespace-only -> 422; CJK accepted | P1 | 1.5 |
| TC-FR01-21 | E | Malformed uuid path | GET /v1/tasks/not-a-uuid | 422 or 404 problem+json (no 500) | P1 | 1.6 |
| TC-FR01-22 | E | Cursor on last page | final cursor | no next cursor | P2 | 1.7 |

### FR-02 Task execution (AC-2.1..2.5)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR02-01 | P | Run returns 202 | POST /v1/tasks/{id}/run, write key | 202, body has run_id | P0 | 2.1 |
| TC-FR02-02 | P | Successful run state machine | command `echo hi` | pending -> running -> done; exit_code 0 | P0 | 2.3 |
| TC-FR02-03 | P | Result persisted | after run | task_results row: exit_code, stdout_tail, stderr_tail, duration_ms, finished_at | P0 | 2.4 |
| TC-FR02-04 | P | Runs history ordering | 3 runs | GET /runs newest first | P0 | 2.5 |
| TC-FR02-05 | P | Uses exec not shell | command `echo a; echo b` | output literal `a;` token, no second command executed; spy confirms create_subprocess_exec(*shlex.split) | P0 | 2.2 |
| TC-FR02-06 | N | Failing command | `false` / `sh -c 'exit 3'` | status failed, exit_code non-zero | P0 | 2.3 |
| TC-FR02-07 | N | Timeout | `sleep 30`, TASKQ_TASK_TIMEOUT=0.5 | status timeout; process killed | P0 | 2.2/2.3 |
| TC-FR02-08 | N | Nonexistent executable | `no_such_bin` | status failed, no 500 | P0 | 2.3 |
| TC-FR02-09 | N | Run unknown task id | random uuid | 404 | P0 | 2.1 |
| TC-FR02-10 | N | Run with read key / GET runs without key | read key; no key | 403; 401 | P0 | 2.1/2.5 |
| TC-FR02-11 | B | Output larger than tail limit | command emitting 1 MB | stdout_tail truncated to tail size, end preserved | P1 | 2.4 |
| TC-FR02-12 | B | Zero-duration command | `true` | duration_ms >= 0 | P2 | 2.4 |
| TC-FR02-13 | E | Unbalanced quote in command | `echo "abc` | shlex error handled: failed status or 422, not 500 | P0 | 2.2 |
| TC-FR02-14 | E | Run twice concurrently | two POST /run same task | two distinct run_ids, both recorded | P1 | 2.1 |
| TC-FR02-15 | E | Runs of task without runs | new task | 200 empty list | P2 | 2.5 |
| TC-FR02-16 | E | stdout with secrets | output `token=abc` | stored tail has line replaced by [REDACTED] | P0 | 2.4 / N4.1 |

### FR-03 API key authentication (AC-3.1..3.5)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR03-01 | P | Valid key accepted | GET /v1/tasks with valid key | 200 | P0 | 3.1 |
| TC-FR03-02 | P | Key stored hashed | `key create --scope read`; query api_keys | key_hash 64 hex, plaintext absent in table | P0 | 3.2 |
| TC-FR03-03 | P | key create prints plaintext once | CLI stdout | plaintext appears exactly once; verifies against hash | P0 | 3.3 |
| TC-FR03-04 | P | Health endpoints open | GET /healthz, /readyz no key | not 401 | P0 | 3.5 |
| TC-FR03-05 | N | Missing header | no X-API-Key | 401 problem+json /errors/unauthenticated | P0 | 3.1 |
| TC-FR03-06 | N | Invalid key | random string | 401 | P0 | 3.1 |
| TC-FR03-07 | N | Revoked key | revoked_at set | 401 | P0 | 3.4 |
| TC-FR03-08 | N | Invalid scope on key create | `--scope root` | non-zero exit, no key created | P1 | 3.3 |
| TC-FR03-09 | B | Empty header value / very long header | "" ; 10 KB | 401, no 500 | P1 | 3.1 |
| TC-FR03-10 | B | Key revoked mid-session | valid then revoke | next request 401 | P1 | 3.4 |
| TC-FR03-11 | E | Constant-time compare | patch/spy hmac.compare_digest | called on hash comparison; no `==` on hashes (grep) | P0 | 3.2 |
| TC-FR03-12 | E | Every /v1 route requires key | iterate app routes | all /v1/* give 401 without key | P0 | 3.1 |
| TC-FR03-13 | E | Header case/whitespace | `x-api-key` lower-case; padded value | lower-case accepted (HTTP), padded value handled without 500 | P2 | 3.1 |
| TC-FR03-14 | E | key create persistence check | scan DB, logs, files after create | plaintext not found anywhere persistent | P0 | 3.3 / N4.3 |

### FR-04 Scope authorization (AC-4.1..4.3)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR04-01 | P | Hierarchy inclusion matrix | read/write/admin keys x all /v1 endpoints | admin passes all; write passes read+write; read passes read only | P0 | 4.1 |
| TC-FR04-02 | N | Insufficient scope | read key POST /v1/tasks; write key DELETE | 403 problem+json /errors/forbidden | P0 | 4.2 |
| TC-FR04-03 | N | No existence leak | write key DELETE existing id vs unknown id | identical 403 status and body shape (modulo instance/correlation_id) | P0 | 4.2 |
| TC-FR04-04 | N | read key on metrics | GET /v1/metrics | 403 | P0 | 4.2 |
| TC-FR04-05 | B | Scope exactly at requirement | write key on write endpoint; read key on read endpoint | allowed | P1 | 4.1 |
| TC-FR04-06 | E | Single dependency | introspect every /v1 route's dependant tree | each includes the same require_scope dependency; none checks scope in handler | P0 | 4.3 |
| TC-FR04-07 | E | Auth before resource lookup | write key DELETE with unknown id | 403 (not 404) | P0 | 4.2 |
| TC-FR04-08 | E | Unknown scope value in DB row | api_keys.scope='x' | treated as no access (401/403), no 500 | P2 | 4.1 |

### FR-05 Rate limiting (AC-5.1..5.4)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR05-01 | P | Within burst | BURST requests quickly | all succeed | P0 | 5.1 |
| TC-FR05-02 | P | Refill | exhaust, wait 1/RATE_PER_SEC (clock injected) | next request succeeds | P0 | 5.1 |
| TC-FR05-03 | P | State in DB | after requests | rate_buckets row for key_id with tokens/updated_at | P0 | 5.3 |
| TC-FR05-04 | N | Over limit | BURST+1 requests | 429 problem+json /errors/rate-limited with Retry-After (integer seconds >=1) | P0 | 5.2 |
| TC-FR05-05 | N | Per-token isolation | key A exhausted | key B still allowed | P0 | 5.1 |
| TC-FR05-06 | B | Exactly BURST then BURST+1 | N=BURST vs N=BURST+1 | Nth ok; (N+1)th 429 | P0 | 5.1/5.2 |
| TC-FR05-07 | B | Tokens never exceed capacity | long idle | tokens <= BURST | P1 | 5.1 |
| TC-FR05-08 | B | BURST=1 / RATE_PER_SEC fractional | env config | 2nd immediate request 429; Retry-After ceil | P1 | 5.1 |
| TC-FR05-09 | E | Health exempt | exhaust key, then /healthz and /readyz (and flood without key) | 200, never 429 | P0 | 5.4 |
| TC-FR05-10 | E | Concurrent requests race | 2*BURST concurrent requests (file SQLite / locking) | accepted count <= BURST (+refill), no over-admit | P0 | 5.3 |
| TC-FR05-11 | E | Rejected counter | 429s | metrics rate-limit rejections increments | P1 | 5.2 / 9.5 |
| TC-FR05-12 | E | Unauthenticated request consumes no bucket | no key flood | 401, no rate_buckets row created | P2 | 5.1 |
| TC-FR05-13 | E | Single transaction with row lock | inspect emitted SQL | SELECT ... FOR UPDATE (PostgreSQL dialect compile) within same transaction as update | P1 | 5.3 |

### FR-06 Persistence and transaction boundary (AC-6.1..6.5)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR06-01 | P | Commit on success | write inside session_scope | row visible from new session | P0 | 6.2 |
| TC-FR06-02 | P | Pool config | build engine with TASKQ_DB_POOL_SIZE=7 | pool size 7, pool_pre_ping True | P0 | 6.5 |
| TC-FR06-03 | P | One Session per request | two requests | distinct sessions; same session within one request | P1 | 6.2 |
| TC-FR06-04 | N | Rollback on exception | raise inside scope after insert | row absent; exception re-raised | P0 | 6.2 |
| TC-FR06-05 | N | Rollback on CancelledError | cancel task inside scope | rolled back, CancelledError propagates | P0 | 6.2 / N3.3 |
| TC-FR06-06 | N | No string SQL | AST/grep over src for f-string/%/+ SQL | 0 hits | P0 | 6.3 |
| TC-FR06-07 | N | Service layer holds no Session | lint-imports + AST check service/api for Session | no sqlalchemy import outside repository/models | P0 | 6.1 |
| TC-FR06-08 | B | Pool exhaustion | pool_size=1, 2 concurrent sessions | second waits/times out with controlled error, not hang forever | P1 | 6.5 |
| TC-FR06-09 | B | Statement count constant for relation load | list 1 vs 50 tasks with tags | same SQL count (selectinload/joinedload) | P0 | 6.4 |
| TC-FR06-10 | E | Stale connection | drop connection between requests | pre_ping recovers | P1 | 6.5 |
| TC-FR06-11 | E | Commit failure | flush raises IntegrityError | rollback, mapped to 409/500 problem, session closed | P1 | 6.2 |
| TC-FR06-12 | E | SQLite and PostgreSQL URL both build | TASKQ_DB_URL variants | engine creates for both dialects (Postgres compile only, no live DB required) | P2 | C-3 |

### FR-07 Alembic migrations (AC-7.1..7.5) — real SQLite file
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR07-01 | P | upgrade head | empty file DB | exit 0; tables tasks, api_keys, tags, task_tags, task_results, rate_buckets exist; tasks.result_json absent | P0 | 7.2 |
| TC-FR07-02 | P | downgrade base | from head | exit 0; no residual tables (except alembic_version) | P0 | 7.2 |
| TC-FR07-03 | P | v3 data migration | at v2 insert tasks with result_json; upgrade v3 | task_results rows with values; result_json column removed | P0 | 7.1 |
| TC-FR07-04 | P | Round-trip fidelity | head -> insert sample -> downgrade -1 -> upgrade head | every column of sample identical | P0 | 7.3 |
| TC-FR07-05 | P | v2 downgrade preserves v1 data | v2 -> v1 | tasks/api_keys rows intact; tags, task_tags and name unique index dropped | P0 | 7.1 |
| TC-FR07-06 | P | Offline SQL | `alembic upgrade --sql` and downgrade --sql | SQL text produced, contains CREATE/DROP of expected objects | P1 | 7.5 |
| TC-FR07-07 | N | Duplicate name blocks v2 index | v1 data with duplicate names, upgrade v2 | controlled failure, rollback, DB remains at v1 | P1 | N3.6 |
| TC-FR07-08 | N | No destructive shortcut | grep migrations for `op.execute("DROP TABLE` as downgrade | 0 hits; downgrade bodies use op.drop_table with data move | P0 | 7.4 |
| TC-FR07-09 | N | Mid-migration failure | inject error in v3 upgrade | transaction rollback, alembic_version still v2 | P0 | N3.6 |
| TC-FR07-10 | B | Empty tables | migrate v3 both directions with no rows | succeeds | P1 | 7.1 |
| TC-FR07-11 | B | Large result_json / many rows | 5000 tasks with JSON | all migrated, count equal both directions | P1 | 7.3 |
| TC-FR07-12 | E | NULL result_json | tasks with NULL | no task_results row (or equivalent); reversed back to NULL | P0 | 7.3 |
| TC-FR07-13 | E | Unicode and nested JSON | CJK/emoji/nested result_json | byte-identical after round trip | P1 | 7.3 |
| TC-FR07-14 | E | Multiple runs per task on downgrade | task with 2 task_results rows | defined deterministic reverse (latest) and documented; no crash | P0 | 7.3 (R1) |
| TC-FR07-15 | E | Idempotent upgrade head twice | run head twice | no-op, exit 0 | P2 | 7.2 |
| TC-FR07-16 | E | Revision chain | alembic heads | exactly one head, chain v1->v2->v3, each has downgrade() | P0 | 7.1 |

### FR-08 Async runner (AC-8.1..8.4)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR08-01 | P | TaskGroup execution | submit 3 short tasks | all complete done | P0 | 8.1 |
| TC-FR08-02 | P | Graceful drain completes | in-flight task shorter than DRAIN_TIMEOUT, shutdown | task finishes done before exit | P0 | 8.1 |
| TC-FR08-03 | P | Concurrency cap | MAX_CONCURRENT=2, submit 6 sleep tasks | max simultaneous running == 2; rest queued, all complete | P0 | 8.2 |
| TC-FR08-04 | N | Drain timeout | task longer than DRAIN_TIMEOUT | status interrupted; subprocess killed; no orphan (pid check) | P0 | 8.1/8.3 |
| TC-FR08-05 | N | Timeout kills process | `sleep 60` with short timeout | process.kill() then await wait(); pid not alive; status timeout | P0 | 8.3 |
| TC-FR08-06 | N | CancelledError propagates | cancel runner task | CancelledError raised to caller, not swallowed | P0 | 8.4 |
| TC-FR08-07 | N | Subprocess spawn failure | missing binary | recorded failed; runner keeps processing other tasks | P1 | 8.1 |
| TC-FR08-08 | B | MAX_CONCURRENT=1 | serial execution | strictly sequential | P1 | 8.2 |
| TC-FR08-09 | B | Queue depth bounded growth | submit 1000 with cap 8 | live coroutines/subprocesses never exceed cap | P1 | 8.2 |
| TC-FR08-10 | B | Timeout boundary | command finishing just before / after timeout | done vs timeout | P2 | 8.3 |
| TC-FR08-11 | E | Shutdown with empty queue | drain with no tasks | returns immediately | P2 | 8.1 |
| TC-FR08-12 | E | Child spawns grandchild | `sh -c 'sleep 60 & wait'` as shlex form | no orphan after kill (process group handling or documented limit) | P1 | 8.3 |
| TC-FR08-13 | E | Cancel during timeout cleanup | cancel while awaiting wait() | process still reaped, CancelledError re-raised | P1 | 8.4 |
| TC-FR08-14 | E | Graceful drain end-to-end via ASGI lifespan | app lifespan exit with running task | interrupted recorded in task_results/state | P0 | 8.1 (N10.3) |
| TC-FR08-15 | E | `interrupted` status vs state machine | assert persisted value | matches NFR-99-2 resolution | P2 | 8.1 |

### FR-09 Health and observability (AC-9.1..9.5)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR09-01 | P | healthz | GET /healthz no key | 200 `{"status":"ok"}` | P0 | 9.1 |
| TC-FR09-02 | P | readyz ready | DB up, at head | 200 | P0 | 9.2 |
| TC-FR09-03 | P | metrics | admin key | 200; task counts by status, latency percentiles, rate-limit rejects | P0 | 9.5 |
| TC-FR09-04 | N | DB down | dispose/stop DB | /readyz 503, detail names DB unavailable, problem+json /errors/not-ready | P0 | 9.3 |
| TC-FR09-05 | N | Not at head | `alembic downgrade -1` | 503, detail names migration not at head | P0 | 9.4 |
| TC-FR09-06 | N | metrics scope | read/write key; no key | 403; 403; 401 | P0 | 9.5 |
| TC-FR09-07 | B | healthz does not touch DB | DB down | /healthz still 200 | P0 | 9.1 |
| TC-FR09-08 | B | Metrics with zero data | empty DB | zeros / null percentiles, no 500 | P1 | 9.5 |
| TC-FR09-09 | E | Both failures | DB down and not at head | 503, detail covers failing check without infinite retry | P1 | N3.4 |
| TC-FR09-10 | E | Metrics no DB URL | grep metrics body for DB URL password | absent | P0 | N4.2 |
| TC-FR09-11 | E | DB error detail not leaking | DB down | detail has no driver message/path/URL | P0 | 10.3 |
| TC-FR09-12 | E | Metrics percentile shape | per NFR-99-4 resolution | documented p50/p95/p99 keys (or confirmed set) | P2 | 9.5 |
| TC-FR09-13 | E | readyz bounded latency | DB unreachable | responds within bounded time (no silent infinite retry) | P1 | N3.4 |

### FR-10 RFC 7807 error contract (AC-10.1..10.6)
| ID | Cat | Description | Input | Expected | Pri | AC |
|----|-----|-------------|-------|----------|-----|----|
| TC-FR10-01 | P | Content type | trigger each of 401/403/404/409/422/429/503/500 | Content-Type application/problem+json | P0 | 10.1 |
| TC-FR10-02 | P | Body fields | each error | type(URI), title, status, detail, instance, correlation_id all present; status equals HTTP code | P0 | 10.2 |
| TC-FR10-03 | P | Type URIs | each error | /errors/validation, unauthenticated, forbidden, not-found, conflict, rate-limited, not-ready, internal | P0 | 10.5 |
| TC-FR10-04 | P | correlation id header + log | any error | X-Correlation-Id equals body.correlation_id and appears in captured log record | P0 | 10.4 |
| TC-FR10-05 | N | 500 hides internals | force unhandled exception with SQL/path in message | detail has no stack, SQL, file path, schema names | P0 | 10.3 |
| TC-FR10-06 | N | DB error leak | force IntegrityError/OperationalError | body clean of SQL and table names | P0 | 10.3 |
| TC-FR10-07 | N | CancelledError | cancel handler | propagates, not converted to 500 | P0 | 10.6 |
| TC-FR10-08 | N | Task timeout not an HTTP error | run times out | run query returns 200 with status timeout (per NFR-99-3 resolution) | P1 | 10.5 |
| TC-FR10-09 | B | Success responses | 2xx | content-type not problem+json | P2 | 10.1 |
| TC-FR10-10 | B | Unknown route / wrong method | GET /nope, PUT /v1/tasks | 404 / 405 returned as problem+json | P1 | 10.1 |
| TC-FR10-11 | E | Multi-error 422 | body with several invalid fields | single problem+json, no pydantic internals / file paths | P1 | 10.3 |
| TC-FR10-12 | E | Client-supplied correlation id | send X-Correlation-Id | echoed or regenerated consistently; injection chars (CRLF) not reflected | P1 | 10.4 |
| TC-FR10-13 | E | Unique correlation ids | 100 errors | all distinct | P2 | 10.4 |
| TC-FR10-14 | E | Error with secrets | exception message containing `postgresql://u:p@h/db` | redacted from body and logs | P0 | N4.1 |
| TC-FR10-15 | E | Middleware-level errors | 429 and 401 raised in middleware/dependency | still same problem+json shape and correlation_id | P0 | 10.2 |

## 2. Non-functional requirement test cases

### NFR-01 Performance (AC-N1.1..N1.4)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR01-01 | P | GET one p95 | pytest-benchmark, 10,000 rows, ASGI transport | p95 < 30 ms | P0 |
| TC-NFR01-02 | P | List limit=50 p95 | 10,000 rows | p95 < 80 ms | P0 |
| TC-NFR01-03 | N | N+1 detection | event listener counts SQL for list with 1, 10, 50 tasks (with tags/results) | count identical (constant) | P0 |
| TC-NFR01-04 | B | limit=200 | 10,000 rows | still constant statement count; report latency | P1 |
| TC-NFR01-05 | E | Last-page cursor at depth | cursor near row 9,950 | latency same order (no offset scan); constant SQL | P1 |
| TC-NFR01-06 | E | Benchmark scope documented | per NFR-99-7 | measured scope recorded in test docstring | P2 |

### NFR-02 Security (AC-N2.1..N2.7)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR02-01 | P | Forbidden constructs grep | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` (excluding create_subprocess_exec) | 0 hits | P0 |
| TC-NFR02-02 | P | bandit | `bandit -r 03-development/src/` | 0 HIGH, 0 MEDIUM | P0 |
| TC-NFR02-03 | P | CORS default deny | TASKQ_CORS_ORIGINS empty; preflight with Origin | no Access-Control-Allow-Origin | P0 |
| TC-NFR02-04 | P | CORS allowlist | origins "https://a.example,https://b.example" | allowed origin echoed; others not | P0 |
| TC-NFR02-05 | N | SQL string building scan | AST scan for f-string/%/+/format feeding text()/execute | 0 hits | P0 |
| TC-NFR02-06 | N | SQL injection payload | name=`'; DROP TABLE tasks;--`, status=`' OR 1=1` | stored literally / rejected; tables intact | P0 |
| TC-NFR02-07 | N | 403 no leak and 500 no leak | see TC-FR04-03, TC-FR10-05 | pass | P0 |
| TC-NFR02-08 | B | CORS with trailing comma/spaces | " https://a , " | parsed to clean list, empty entries dropped | P1 |
| TC-NFR02-09 | E | Wildcard origin | TASKQ_CORS_ORIGINS="*" | behavior defined (documented); not silently allow-all with credentials | P1 |
| TC-NFR02-10 | E | Default bind address | TASKQ_HOST unset | 127.0.0.1 | P1 |

### NFR-03 Error handling and async correctness (AC-N3.1..N3.6)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR03-01 | P | AST: no bare except / except Exception: pass | scan src | 0 hits | P0 |
| TC-NFR03-02 | P | Commit/rollback | see TC-FR06-01/04 | pass | P0 |
| TC-NFR03-03 | N | CancelledError never swallowed | AST: every `except Exception/BaseException` in async code re-raises or CancelledError handled before; behavioural cancel tests (TC-FR08-06) | pass | P0 |
| TC-NFR03-04 | N | DB failure readyz | see TC-FR09-04/13 | 503, no infinite retry | P0 |
| TC-NFR03-05 | N | Migration failure rollback | see TC-FR07-09 | previous revision retained | P0 |
| TC-NFR03-06 | B | No orphan processes | after timeout/drain/cancel tests, enumerate child pids | 0 children left | P0 |
| TC-NFR03-07 | E | `except BaseException` usage | AST | none, or re-raises | P1 |

### NFR-04 Redaction (AC-N4.1..N4.3)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR04-01 | P | Each pattern redacted | lines with `sk-ABCDEFGH12`, `token=xyz`, `Bearer abc.def`, `postgresql://u:p@h/db`, `postgres://...` | whole line becomes `[REDACTED]` | P0 |
| TC-NFR04-02 | P | Non-matching lines kept | normal lines mixed in | unchanged | P0 |
| TC-NFR04-03 | P | Applied to stdout_tail, stderr_tail, logs, error bodies | run command echoing secret | none persisted/sent raw | P0 |
| TC-NFR04-04 | N | DB URL never in logs/metrics/errors | startup with password URL; collect logs, /v1/metrics, forced 500 | password fragment absent | P0 |
| TC-NFR04-05 | N | API key plaintext not persisted | after key create + usage | absent from DB, logs, files | P0 |
| TC-NFR04-06 | B | `sk-` with 7 vs 8 chars | `sk-1234567` / `sk-12345678` | 7 kept, 8 redacted | P0 |
| TC-NFR04-07 | B | Pattern at line start/end, multiple per line, multi-line payload | | only matching lines replaced; line count preserved | P1 |
| TC-NFR04-08 | E | Case variants | `TOKEN=abc`, `bearer abc` | behavior matches regex exactly (case-sensitive per SPEC), documented | P1 |
| TC-NFR04-09 | E | Catastrophic backtracking | 1 MB line near-miss | completes < 1 s | P1 |
| TC-NFR04-10 | E | Redaction applied after tail truncation boundary | secret split across tail cut | no partial secret leaks | P1 |

### NFR-05 Documentation (AC-N5.1..N5.2)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR05-01 | P | Docstring coverage | AST scan public def/class | 100% with docstring | P0 |
| TC-NFR05-02 | P | Reference tag | each docstring | contains `[FR-XX]` or `[NFR-XX]` | P0 |
| TC-NFR05-03 | P | OpenAPI summary/description | GET /openapi.json | every operation has non-empty summary and description | P0 |
| TC-NFR05-04 | N | Missing doc detection | synthetic module without docstring | checker flags it (counterexample) | P1 |
| TC-NFR05-05 | E | Dunder/private/nested/async defs | scan config | rules documented; async public funcs included | P2 |

### NFR-06 Layering (AC-N6.1..N6.4)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR06-01 | P | lint-imports | run | exit 0 | P0 |
| TC-NFR06-02 | P | Contract content | parse `.importlinter` | layers api>service>repository>models; independence config/errors; forbidden sqlalchemy | P0 |
| TC-NFR06-03 | N | Seeded violation (service imports sqlalchemy) | temp copy of project | lint-imports non-zero | P0 |
| TC-NFR06-04 | N | Seeded upward import (repository imports service) | temp copy | non-zero | P0 |
| TC-NFR06-05 | E | No wildcard ignore_imports; file present | grep `.importlinter` | 0 wildcard ignores | P0 |
| TC-NFR06-06 | E | config and errors independent | seed config->errors import | non-zero | P1 |

### NFR-07 License and dependencies (AC-N7.1..N7.4)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR07-01 | P | Pinning | parse requirements.txt | every line uses `==` | P0 |
| TC-NFR07-02 | P | Lock completeness | requirements.lock vs `pip freeze` of .venv | every installed transitive dep pinned in lock | P0 |
| TC-NFR07-03 | P | License allowlist | `pip-licenses --format=json --with-system` | every license in MIT/BSD-2/BSD-3/Apache-2.0/PSF (normalized aliases documented) | P0 |
| TC-NFR07-04 | P | SBOM | read 08-config/SBOM.json | each dep has name, version, license, direct/transitive | P0 |
| TC-NFR07-05 | N | Disallowed license detection | synthetic GPL entry | checker fails (counterexample) | P1 |
| TC-NFR07-06 | B | SBOM matches lock | counts | SBOM entries == lock entries | P1 |
| TC-NFR07-07 | E | Dual / composite license strings | "Apache-2.0 OR MIT", "BSD License" | classified per documented rule; UNKNOWN fails | P1 |

### NFR-08 Mutation testing (AC-N8.1..N8.3)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR08-01 | P | Config flag | harness_config.json | features.mutation_testing true | P0 |
| TC-NFR08-02 | P | Score | `mutmut run`; `mutmut results` | score >= 70 | P0 |
| TC-NFR08-03 | P | Scope | mutmut paths | only service/ and repository/ | P0 |
| TC-NFR08-04 | N | Rationale recorded | harness_config.json | scope-restriction rationale (time budget) present | P1 |
| TC-NFR08-05 | E | Survivors in high-risk modules | runner, auth, session | survivors triaged and listed; none equivalent-mutant masked silently | P1 |
| TC-NFR08-06 | E | Allowed exclusions | standalone-mutmut rules | only documented exclusions applied | P2 |

### NFR-09 Verification authenticity (AC-N9.1..N9.6)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR09-01 | P | Zero skipped | `pytest 03-development/tests -q` | skipped = 0, all pass | P0 |
| TC-NFR09-02 | P | Zero zero-assert tests | AST: every test_ function has assert / pytest.raises | 0 offenders | P0 |
| TC-NFR09-03 | P | No skip/xfail markers | grep `pytest.skip`, `skipif`, `xfail` | 0 hits | P0 |
| TC-NFR09-04 | N | No exclusion flags | inspect pytest.ini/pyproject/Makefile/conftest | no --ignore, -k, --deselect, collect_ignore; testpaths includes all dirs | P0 |
| TC-NFR09-05 | N | Migration tests use file DB | grep migration tests | sqlite file path used, no `:memory:` | P0 |
| TC-NFR09-06 | B | 100% line coverage | `pytest --cov=03-development/src` | TOTAL 100% (SPEC §8 #2) | P0 |
| TC-NFR09-07 | E | VERIFIED honesty | TRACEABILITY_MATRIX.md vs last pytest results | every VERIFIED row maps to a passing test | P1 |
| TC-NFR09-08 | E | Stub tests | AST: assert True / pass-only bodies | none | P1 |

### NFR-10 Integration coverage (AC-N10.1..N10.3)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR10-01 | P | Coverage | `pytest 03-development/tests/integration --cov=03-development/src` | TOTAL >= 80% | P0 |
| TC-NFR10-02 | P | Transport | grep integration tests | all use ASGITransport(app); no direct handler call | P0 |
| TC-NFR10-03 | P | Required scenarios present | CRUD chain; 401/403/404/409/422/429/503 one each; migration round trip; rate limit trigger+recovery; graceful drain | each has at least one passing integration test | P0 |
| TC-NFR10-04 | N | Handler direct-call detection | AST for imports of route functions in integration dir | 0 | P1 |
| TC-NFR10-05 | E | Isolation | run integration tests in random order / repeated | pass; no shared state leakage | P1 |

### NFR-11 Readability (AC-N11.1..N11.4)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR11-01 | P | MI | radon mi / readability-v2 | >= 80 (LLOC-weighted per NFR-99-5) | P0 |
| TC-NFR11-02 | P | CC | radon cc | every function <= 10 | P0 |
| TC-NFR11-03 | P | File size | wc -l | every file <= 400 lines | P0 |
| TC-NFR11-04 | P | Directory size | count files per dir | <= 15 | P0 |
| TC-NFR11-05 | P | Handler length | AST on api/ handlers | <= 40 lines each | P0 |
| TC-NFR11-06 | B | Exactly at limits | synthetic 10 CC / 400 lines / 15 files / 40 lines | pass; +1 fails (checker self-test) | P2 |

### NFR-12 System verification (AC-N12.1..N12.2)
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR12-01 | P | verify-system | `make verify-system` | exit 0; stdout contains `verify-system: PASS` | P0 |
| TC-NFR12-02 | P | Target step list | read Makefile | upgrade head; full tests; start service + /healthz and /readyz smoke; downgrade base then upgrade head | P0 |
| TC-NFR12-03 | N | Failure propagation | induce failing step (temp copy) | make exits non-zero, no PASS line | P0 |
| TC-NFR12-04 | E | Clean state / re-runnable | run twice; port occupied | second run passes; service process stopped afterwards | P1 |
| TC-NFR12-05 | E | .env.example | `grep -c "^TASKQ_" .env.example` | 12 (SPEC §8 #26) | P1 |
| TC-NFR12-06 | E | Env var defaults | unset all; load config | defaults per SRS 2.1; invalid values (LOG_LEVEL=TRACE, PORT=abc) rejected with clear error | P1 |

### NFR-99 Ambiguity resolution
| ID | Cat | Description | Input | Expected | Pri |
|----|-----|-------------|-------|----------|-----|
| TC-NFR99-01 | E | Each of 7 open items has recorded stakeholder resolution or a bug-hunt/flag entry | SRS section 7 vs 04-testing notes | no item silently assumed; unresolved ones flagged "Requires Verification" in TRACEABILITY_MATRIX | P1 |

## 3. Cross-cutting scenarios (SPEC §8 acceptance mapping)
| SPEC §8 | Covered by |
|---|---|
| #1 | TC-NFR09-01 |
| #2 | TC-NFR09-06 |
| #3 | TC-NFR10-01 |
| #4..#9 | TC-FR01-01, TC-FR03-05, TC-FR04-03, TC-FR01-09, TC-FR01-10, TC-FR05-04 |
| #10, #11 | TC-FR09-04, TC-FR09-05 |
| #12, #13 | TC-FR07-04, TC-FR07-02 |
| #14, #15 | TC-NFR01-03, TC-NFR01-01 |
| #16..#18 | TC-NFR02-01, TC-NFR02-05, TC-FR03-02 |
| #19, #20 | TC-FR10-05, TC-NFR04-04 |
| #21..#24 | TC-NFR06-01/03, TC-NFR07-03, TC-NFR02-02, TC-NFR08-02 |
| #25 | TC-FR08-04, TC-FR08-14 |
| #26, #27 | TC-NFR12-05, TC-NFR12-01 |

## 4. High-risk module focus (per-module extra depth)
- `taskq_api.service.runner`: TC-FR08-*, TC-FR02-07/13, TC-NFR03-06.
- `taskq_api.service.auth`: TC-FR03-*, TC-FR04-*.
- `taskq_api.repository.session`: TC-FR06-*, TC-NFR03-02.
- `migrations.versions.v3_split_results`: TC-FR07-03/04/09/11..14.
- Async scanner misjudgments found during Phase 4 are recorded in the bug hunt, not bypassed.

## 5. Coverage summary
All FRs in manifest (FR-01..FR-10) have P/N/B/E cases; all NFR-01..NFR-12 and NFR-99 have cases. Priority P0 cases gate Phase 4 exit (Gate 3).
