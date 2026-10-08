# Software Requirements Specification (SRS) — taskq-api

Source: `SPEC.md` v1.0.0 (10 FR / 12 NFR / 12 env). Language: Python 3.11. Phase 1.

## 1. Introduction

- **Purpose**: 任務佇列的 HTTP 服務化 — 以 REST API 提交、查詢、執行任務;資料持久化於關聯式資料庫;schema 隨版本演進;支援認證、授權與流量控制 (SPEC.md:52).
- **Form**: ASGI 服務,`uvicorn taskq_api.app:app` 啟動;另提供 `python -m taskq_api` 管理入口(migrate / seed / healthcheck) (SPEC.md:54).
- **Source of truth**: project-root `SPEC.md`. Citations use `SPEC.md:<line>`.

## 2. Constraints

| ID | Constraint | Citation |
|----|-----------|----------|
| C-1 | HTTP framework FastAPI (ASGI); pydantic v2 request/response models | SPEC.md:62-63 |
| C-2 | ORM SQLAlchemy 2.x (declarative + `Session` 明確交易邊界) | SPEC.md:64 |
| C-3 | DB: SQLite (開發/測試), PostgreSQL (生產), 同一份 ORM 模型 | SPEC.md:65 |
| C-4 | Migration: Alembic (v1 → v2 → v3, 每步都有 `downgrade`) | SPEC.md:66 |
| C-5 | Async: `async def` 端點 + `asyncio.TaskGroup` 背景執行器 | SPEC.md:67 |
| C-6 | Auth: `X-API-Key` header, 金鑰雜湊後比對 (不存明文); scopes `read` / `write` / `admin` | SPEC.md:68-69 |
| C-7 | Rate limit: per-token 令牌桶 | SPEC.md:70 |
| C-8 | Errors: RFC 7807 `application/problem+json` | SPEC.md:71 |
| C-9 | Task execution: `asyncio.create_subprocess_exec` (禁 `shell=True`) | SPEC.md:72 |
| C-10 | Layering: `import-linter` layers contract (NFR-06) | SPEC.md:73 |
| C-11 | Config via 12 `TASKQ_*` environment variables (§2.1 below, SPEC §5.1) | SPEC.md:287-302 |
| C-12 | DB schema defined by Alembic revisions (SPEC §5.2) | SPEC.md:304-315 |
| C-13 | Required project-side files: `.importlinter`, `requirements.txt` + `requirements.lock`, `requirements-dev.txt`, `alembic.ini` + `migrations/versions/`, `.env.example`, `.methodology/harness_config.json`, `Makefile` | SPEC.md:317-327 |

### 2.1 Environment variables (SPEC §5.1, SPEC.md:289-302)

| Variable | Default | Description |
|---|---|---|
| `TASKQ_DB_URL` | `sqlite:///./taskq.db` | 資料庫連線字串 (不得出現在日誌 — NFR-04) |
| `TASKQ_DB_POOL_SIZE` | `5` | 連線池大小 (FR-06) |
| `TASKQ_TASK_TIMEOUT` | `10.0` | 單任務 subprocess timeout (秒) |
| `TASKQ_MAX_CONCURRENT` | `8` | 背景執行併發上限 (FR-08) |
| `TASKQ_DRAIN_TIMEOUT` | `30.0` | 關閉時 graceful drain 上限 (秒) |
| `TASKQ_RATE_BURST` | `20` | 令牌桶容量 (FR-05) |
| `TASKQ_RATE_PER_SEC` | `5.0` | 令牌補充速率 (FR-05) |
| `TASKQ_CORS_ORIGINS` | (空字串) | CORS 允許來源, 逗號分隔; 空 = 全拒 (NFR-02) |
| `TASKQ_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `TASKQ_LOG_FORMAT` | `json` | `json` / `text` |
| `TASKQ_HOST` | `127.0.0.1` | 監聽位址 (預設不對外) |
| `TASKQ_PORT` | `8000` | 監聽埠 |

### 2.2 Database schema (SPEC §5.2, SPEC.md:306-315)

| Table | Revision | Main columns |
|---|---|---|
| `tasks` | v1 | `id`(uuid), `command`, `name`, `status`, `created_at` |
| `api_keys` | v1 | `id`, `key_hash`(sha256), `scope`, `created_at`, `revoked_at` |
| `tags` | v2 | `id`, `label` |
| `task_tags` | v2 | `task_id`, `tag_id` (複合主鍵) |
| `task_results` | v3 | `id`, `task_id`(FK), `exit_code`, `stdout_tail`, `stderr_tail`, `duration_ms`, `finished_at` |
| `rate_buckets` | v1 | `key_id`(FK), `tokens`, `updated_at` |

`tasks.result_json` 在 v1 建立、v3 移除 (資料搬遷至 `task_results`) (SPEC.md:315).

## 3. Functional Requirements

### FR-01: 任務資源 CRUD API

Citation: SPEC.md:79-91.

| 方法 | 路徑 | scope | 行為 |
|------|------|-------|------|
| `POST` | `/v1/tasks` | `write` | 建立任務; body 由 `TaskCreate` pydantic 模型驗證 |
| `GET` | `/v1/tasks/{id}` | `read` | 取得單一任務全欄位 |
| `GET` | `/v1/tasks` | `read` | 分頁列表, 支援 `?status=`、`?limit=`、`?cursor=` |
| `DELETE` | `/v1/tasks/{id}` | `admin` | 刪除任務 (連同結果列, 同一交易) |

**Acceptance criteria (FR-01)**
- **AC-1.1**: `POST /v1/tasks` requires scope `write`, validates body by `TaskCreate` pydantic model; valid request with a write key → 201 + task id — decided by SPEC §8 #4 (SPEC.md:360).
- **AC-1.2**: `GET /v1/tasks/{id}` requires scope `read` and returns 取得單一任務全欄位 (SPEC.md:84).
- **AC-1.3**: `GET /v1/tasks` requires scope `read` and supports `?status=`、`?limit=`、`?cursor=` (SPEC.md:85).
- **AC-1.4**: `DELETE /v1/tasks/{id}` requires scope `admin` and deletes the task 連同結果列, 同一交易 (SPEC.md:86).
- **AC-1.5**: 驗證規則同第 1 輪 FR-01 (非空 / ≤1000 字元 / 注入字元黑名單 / 名稱唯一); 違反 → HTTP 422 + problem+json (SPEC.md:88). The 注入字元黑名單 content is not defined in SPEC.md — see NFR-99 item 1.
- **AC-1.6**: 未知 id → HTTP 404 + problem+json — decided by SPEC §8 #7 (SPEC.md:363).
- **AC-1.7**: 分頁為 cursor-based (不得用 offset) (SPEC.md:90).
- **AC-1.8**: 列表端點的預設 `limit` 為 50, 上限 200; 超過上限 → 422 (SPEC.md:91).
- **AC-1.9**: `POST /v1/tasks` with duplicate `name` → 409 — decided by SPEC §8 #8 (SPEC.md:364).

### FR-02: 任務執行端點

Citation: SPEC.md:93-99.

**Acceptance criteria (FR-02)**
- **AC-2.1**: `POST /v1/tasks/{id}/run` (scope `write`) → HTTP 202 Accepted, body 含 `run_id` (SPEC.md:95).
- **AC-2.2**: 實際執行以 `asyncio.create_subprocess_exec(*shlex.split(command))` 進行, 禁 `shell=True`, timeout 為 `TASKQ_TASK_TIMEOUT` (SPEC.md:96).
- **AC-2.3**: 狀態機: `pending → running → done | failed | timeout` (SPEC.md:97).
- **AC-2.4**: 執行結果寫入 `task_results` 表 (FR-07 的 v3 schema), 欄位: `exit_code` / `stdout_tail` / `stderr_tail` / `duration_ms` / `finished_at` (SPEC.md:98).
- **AC-2.5**: `GET /v1/tasks/{id}/runs` (scope `read`) → 該任務的歷史執行紀錄, 新到舊排序 (SPEC.md:99).

### FR-03: API Key 認證

Citation: SPEC.md:101-107.

**Acceptance criteria (FR-03)**
- **AC-3.1**: 全部 `/v1/*` 端點要求 `X-API-Key` header; 缺少或無效 → HTTP 401 + problem+json — decided by SPEC §8 #5 (SPEC.md:361).
- **AC-3.2**: 金鑰以 SHA-256 雜湊儲存於 `api_keys` 表, 不得存明文; 比對用 `hmac.compare_digest` (常數時間) — decided by SPEC §8 #18 (查 `api_keys` 表: 無明文金鑰; `key_hash` 為 64 hex) (SPEC.md:104, 374).
- **AC-3.3**: 金鑰由 `python -m taskq_api key create --scope <scope>` 產生, 明文只在建立當下印出一次 (SPEC.md:105).
- **AC-3.4**: `revoked_at` 非空的金鑰一律視為無效 (SPEC.md:106).
- **AC-3.5**: `/healthz`、`/readyz` 不要求認證 (FR-09) (SPEC.md:107).

### FR-04: Scope 授權

Citation: SPEC.md:109-113.

**Acceptance criteria (FR-04)**
- **AC-4.1**: 每把金鑰帶一個 scope: `read` < `write` < `admin` (階層包含) (SPEC.md:111).
- **AC-4.2**: 端點所需 scope 見 FR-01/02 表; 不足 → HTTP 403 + problem+json, 且 body 不得洩漏該資源是否存在 — decided by SPEC §8 #6 (write key 呼叫 `DELETE /v1/tasks/{id}` → 403, body 不透露該 id 是否存在) (SPEC.md:112, 362).
- **AC-4.3**: 授權判定必須在單一中介層 (dependency) 完成, 不得散落於各 handler — 以測試斷言「每個 `/v1` 路由都經過同一個 dependency」 (SPEC.md:113).

### FR-05: 流量控制

Citation: SPEC.md:115-120.

**Acceptance criteria (FR-05)**
- **AC-5.1**: per-token 令牌桶: 容量 `TASKQ_RATE_BURST`, 補充速率 `TASKQ_RATE_PER_SEC` (SPEC.md:117).
- **AC-5.2**: 超限 → HTTP 429 + problem+json + `Retry-After` header (秒) — decided by SPEC §8 #9 (SPEC.md:118, 365).
- **AC-5.3**: 令牌桶狀態存於資料庫 (跨 worker 一致), 更新必須在單一交易內以 row-level lock 進行 (SPEC.md:119).
- **AC-5.4**: `/healthz`、`/readyz` 不受限 (SPEC.md:120).

### FR-06: 持久化層與交易邊界

Citation: SPEC.md:122-128.

**Acceptance criteria (FR-06)**
- **AC-6.1**: 全部資料存取經由 `repository/` 層, 業務層不得直接持有 `Session` (SPEC.md:124).
- **AC-6.2**: 每個 API 請求一個 `Session`, 交易邊界明確: 成功 commit、例外 rollback (以 context manager 保證) (SPEC.md:125).
- **AC-6.3**: 禁止字串拼接 SQL; 一律使用 ORM 或參數化查詢 (NFR-02) (SPEC.md:126).
- **AC-6.4**: 關聯查詢必須用 `selectinload` / `joinedload` 顯式預載 — N+1 為驗收失敗條件 (NFR-01) (SPEC.md:127).
- **AC-6.5**: 連線池: `pool_size=TASKQ_DB_POOL_SIZE`, `pool_pre_ping=True` (SPEC.md:128).

### FR-07: Schema Migration(Alembic 三步演進)

Citation: SPEC.md:130-143.

| revision | upgrade 內容 | downgrade 要求 |
|---|---|---|
| v1 | 建立 `tasks`、`api_keys` 兩表 | drop 兩表 |
| v2 | 新增 `tags`、`task_tags`(多對多) + `tasks.name` 唯一索引 | drop 新表與索引, 不影響 v1 資料 |
| v3 | 含資料搬遷: 把 `tasks.result_json` 拆為獨立的 `task_results` 表, 搬遷既有資料後移除原欄位 | 反向搬遷回 `tasks.result_json` 後 drop `task_results`, 資料不得遺失 |

**Acceptance criteria (FR-07)**
- **AC-7.1**: 三個 revision (v1, v2, v3), 每一步都必須有可運作的 `downgrade` (SPEC.md:132).
- **AC-7.2**: `alembic upgrade head` 與 `alembic downgrade base` 必須都成功 — decided by SPEC §8 #13 (`alembic downgrade base`: exit 0, 無殘留表) (SPEC.md:140, 369).
- **AC-7.3**: 往返可逆性驗收: `upgrade head` → 寫入樣本資料 → `downgrade -1` → `upgrade head`, 樣本資料的欄位值必須逐欄相同 — decided by SPEC §8 #12 (SPEC.md:141, 368).
- **AC-7.4**: 禁止以 `op.execute("DROP TABLE ...")` 之類的破壞性捷徑取代真正的 downgrade (SPEC.md:142).
- **AC-7.5**: migration 檔本身納入測試覆蓋 (以 `alembic` 的 offline SQL 產生 + 斷言) (SPEC.md:143).

### FR-08: 非同步執行器

Citation: SPEC.md:145-150.

**Acceptance criteria (FR-08)**
- **AC-8.1**: 背景執行以 `asyncio.TaskGroup` 管理; 服務關閉時必須 graceful drain (等待進行中的任務至 `TASKQ_DRAIN_TIMEOUT`, 逾時則標記 `interrupted`) — decided by SPEC §8 #25 (SPEC.md:147, 381). The `interrupted` status is absent from FR-02 state machine — see NFR-99 item 2.
- **AC-8.2**: 併發上限 `TASKQ_MAX_CONCURRENT`; 超過時新任務排隊, 不得無限制生成 coroutine (SPEC.md:148).
- **AC-8.3**: 任務 timeout 以 `asyncio.wait_for` 實作; 逾時必須確實終止子進程 (`process.kill()` 後 `await process.wait()`), 不得留下孤兒進程 (SPEC.md:149).
- **AC-8.4**: 取消語意: `asyncio.CancelledError` 必須向上傳播, 不得被 `except Exception` 吞掉 (NFR-03) (SPEC.md:150).

### FR-09: 健康檢查與可觀測性

Citation: SPEC.md:152-160.

| 端點 | 認證 | 行為 |
|------|------|------|
| `GET /healthz` | 無 | 進程存活 → 200 `{"status":"ok"}` |
| `GET /readyz` | 無 | DB 連線可用 且 `alembic current` == head → 200; 否則 503 並在 body 說明哪一項失敗 |
| `GET /v1/metrics` | `admin` | 任務計數(按狀態)、執行延遲分位數、rate-limit 拒絕數 |

**Acceptance criteria (FR-09)**
- **AC-9.1**: `GET /healthz` (無認證) 進程存活 → 200 `{"status":"ok"}` (SPEC.md:156).
- **AC-9.2**: `GET /readyz` (無認證): DB 連線可用 且 `alembic current` == head → 200; 否則 503 並在 body 說明哪一項失敗 (SPEC.md:157).
- **AC-9.3**: 停掉 DB 後 `GET /readyz` → 503, detail 指明 DB 不可用 — decided by SPEC §8 #10 (SPEC.md:366).
- **AC-9.4**: `alembic downgrade -1` 後 `GET /readyz` → 503, detail 指明 migration 未到 head (fail closed) — decided by SPEC §8 #11 (SPEC.md:160, 367).
- **AC-9.5**: `GET /v1/metrics` (scope `admin`) 回傳 任務計數(按狀態)、執行延遲分位數、rate-limit 拒絕數 (SPEC.md:158). The percentile set is unspecified — see NFR-99 item 4.

### FR-10: 錯誤契約(RFC 7807)

Citation: SPEC.md:162-168, 331-347.

| 情況 | HTTP | `type` |
|------|------|--------|
| 請求 body 驗證失敗 | 422 | `/errors/validation` |
| 缺少或無效 API key | 401 | `/errors/unauthenticated` |
| scope 不足 | 403 | `/errors/forbidden` (不洩漏資源是否存在) |
| 未知 task id | 404 | `/errors/not-found` |
| 任務名稱衝突 | 409 | `/errors/conflict` |
| 超過 rate limit | 429 | `/errors/rate-limited` (附 `Retry-After`) |
| DB 不可用 / migration 未到 head | 503 | `/errors/not-ready` |
| 任務 timeout | 200 (任務狀態 `timeout`) | — |
| 其他未預期例外 | 500 | `/errors/internal` (detail 不含堆疊/SQL/路徑) |

**Acceptance criteria (FR-10)**
- **AC-10.1**: 全部非 2xx 回應的 `Content-Type` 為 `application/problem+json` (SPEC.md:164).
- **AC-10.2**: body 欄位: `type`(URI)、`title`、`status`、`detail`、`instance`、`correlation_id` (SPEC.md:165).
- **AC-10.3**: `detail` 不得洩漏內部細節: 不得含 SQL 陳述、堆疊追蹤、檔案路徑、資料庫結構描述 — decided by SPEC §8 #19 (觸發 500 後檢查回應 body) (SPEC.md:166, 375).
- **AC-10.4**: `correlation_id` 同時出現在回應 header `X-Correlation-Id` 與伺服器日誌, 可用於串接 (SPEC.md:167).
- **AC-10.5**: 錯誤碼對照: 422 驗證 / 401 未認證 / 403 scope 不足 / 404 未知資源 / 409 名稱衝突 / 429 超限 / 503 未就緒 / 500 其他, with the `type` URIs of the table above (SPEC.md:168, 335-345).
- **AC-10.6**: `asyncio.CancelledError` 不屬於上表任何一列 — 它必須向上傳播, 不得轉成 500 (NFR-03) (SPEC.md:347).

## 4. Non-Functional Requirements

Dimension check: every `dimension` below was grepped against the `### <dimension>` headers of `harness/harness/ssi/prompts/evaluate_dimension.md`; all 12 are present in the current roster, so no dimension note is required. Coverage notes record where that file's check is narrower than the AC.

### NFR-01: 效能與查詢效率

Citation: SPEC.md:177-183. dimension: `performance`.

**Acceptance criteria (NFR-01)**
- **AC-N1.1**: `GET /v1/tasks/{id}` 在 10,000 筆資料下 p95 < 30ms (不含網路, 以 ASGI transport 量測) — decided by SPEC §8 #15 and `pytest-benchmark` (SPEC.md:180, 371, 437).
- **AC-N1.2**: `GET /v1/tasks?limit=50` 在 10,000 筆資料下 p95 < 80ms — decided by `pytest-benchmark` (SPEC.md:181, 438).
- **AC-N1.3**: N+1 為失敗條件: 列表端點回應一次請求所發出的 SQL 陳述數必須是常數 (與回傳筆數無關), 以 SQLAlchemy event listener 計數斷言 — decided by SPEC §8 #14 (SPEC.md:182, 370).
- **AC-N1.4**: 量測方式: `pytest-benchmark` (SPEC.md:183).

**Coverage note (AC-N1.1, AC-N1.2)**: evaluate_dimension.md `performance` scores benchmark means (penalty only above 1000 ms) and judges typed latency `targets` declared in the SRS JSON block; the targets below carry those numbers. **Coverage note (AC-N1.3)**: the `performance` section does not verify SQL statement count; AC-N1.3 needs a dedicated implementation task (event-listener test).

### NFR-02: HTTP 與資料層安全

Citation: SPEC.md:185-194. dimension: `security`.

**Acceptance criteria (NFR-02)**
- **AC-N2.1**: 全 codebase 禁用 `shell=True`、`eval(`、`exec(` (grep 0 命中) — decided by SPEC §8 #16 (SPEC.md:187, 372).
- **AC-N2.2**: 禁止字串拼接 SQL: 不得出現 f-string / `%` / `+` 組成的 SQL; 一律 ORM 或參數化 (以 grep + code review 雙重驗證) — decided by SPEC §8 #17 (SPEC.md:188-189, 373).
- **AC-N2.3**: API key 雜湊儲存, 比對用 `hmac.compare_digest` (FR-03) (SPEC.md:190).
- **AC-N2.4**: 403 回應不得洩漏資源存在性 (FR-04) (SPEC.md:191).
- **AC-N2.5**: 錯誤 body 不得含堆疊/SQL/路徑 (FR-10) (SPEC.md:192).
- **AC-N2.6**: CORS 預設拒絕所有來源; 允許清單由 `TASKQ_CORS_ORIGINS` 明示 (SPEC.md:193).
- **AC-N2.7**: `bandit -r 03-development/src/`: 0 HIGH、0 MEDIUM — decided by SPEC §8 #23 (SPEC.md:194, 379).

**Coverage note (AC-N2.1 to AC-N2.6)**: evaluate_dimension.md `security` runs bandit only and scores `100 - HIGH×10 - MEDIUM×3 - LOW×1`; it does not grep for SQL string concatenation, check CORS, hashing or 403 leakage, and the score can be above 0 with HIGH/MEDIUM findings. AC-N2.2 to AC-N2.6 need dedicated implementation tasks (grep gate and integration tests); AC-N2.7 requires 0/0 whereas the gate formula only deducts.

### NFR-03: 錯誤處理、交易與非同步正確性

Citation: SPEC.md:196-204. dimension: `error_handling`.

**Acceptance criteria (NFR-03)**
- **AC-N3.1**: 每個請求的交易邊界明確: 成功 commit、例外 rollback, 以 context manager 保證 (FR-06) (SPEC.md:199).
- **AC-N3.2**: 不得出現裸 `except:`、`except Exception: pass` (SPEC.md:200).
- **AC-N3.3**: `asyncio.CancelledError` 不得被吞掉 — 必須重新拋出 (SPEC.md:201).
- **AC-N3.4**: 資料庫連線失敗 → `/readyz` 503 + 明確 detail; 不得靜默重試至無限 (SPEC.md:202).
- **AC-N3.5**: 任務 timeout 必須確實終止子進程, 不留孤兒 (FR-08) (SPEC.md:203).
- **AC-N3.6**: migration 失敗 → 交易 rollback, 資料庫維持在前一個 revision (FR-07) (SPEC.md:204).

**Coverage note (AC-N3.1, AC-N3.3 to AC-N3.6)**: the `error_handling` section scores file-level handler coverage and deducts for anti-patterns (`bare_except`, `broad_swallow`, `except_base_exception`), which covers AC-N3.2 and part of AC-N3.3 only; it does not verify commit/rollback, `/readyz` behaviour, orphan processes or migration rollback. Those need dedicated implementation tasks.

### NFR-04: 敏感資料遮蔽

Citation: SPEC.md:206-212. dimension: `security`.

**Acceptance criteria (NFR-04)**
- **AC-N4.1**: `stdout_tail` / `stderr_tail` / 日誌 / 錯誤 body 落盤或送出前, 匹配 `(sk-[A-Za-z0-9_-]{8,}|token=\S+|Bearer\s+\S+|postgres(ql)?://[^\s]+)` 的行整行以 `[REDACTED]` 取代 (SPEC.md:209-210).
- **AC-N4.2**: 資料庫連線字串 (含密碼) 不得出現在任何日誌、錯誤訊息或 `/v1/metrics` 回應中 — decided by SPEC §8 #20 (SPEC.md:211, 376).
- **AC-N4.3**: API key 明文只在 `key create` 當下輸出一次, 不得寫入任何持久化位置 (SPEC.md:212).

**Coverage note**: bandit (the `security` check) does not verify redaction; AC-N4.1 to AC-N4.3 need dedicated implementation tasks (unit tests, SPEC.md:452).

### NFR-05: 文件覆蓋

Citation: SPEC.md:214-218. dimension: `documentation`.

**Acceptance criteria (NFR-05)**
- **AC-N5.1**: 全部公開函式/類別有 docstring 且含 `[FR-XX]` 或 `[NFR-XX]` 引用, 覆蓋率 100% (SPEC.md:217).
- **AC-N5.2**: 每個 API 端點在 OpenAPI schema 中有 `summary` 與 `description` (FastAPI 自動產生的 `/openapi.json` 以測試斷言) (SPEC.md:218).

**Coverage note**: the `documentation` section (ast-docstrings) measures docstring presence on public def/class; it does not check for `[FR-XX]`/`[NFR-XX]` references nor OpenAPI summary/description. AC-N5.1 (reference part) and AC-N5.2 need dedicated implementation tasks.

### NFR-06: 架構分層契約

Citation: SPEC.md:220-232. dimension: `architecture_constraints`.

**Acceptance criteria (NFR-06)**
- **AC-N6.1**: 專案根目錄必須存在 `.importlinter`, 宣告 layers contract `api > service > repository > models`; 上層可 import 下層, 下層不得 import 上層; `config` 與 `errors` 為 independence 模組 (SPEC.md:222-229).
- **AC-N6.2**: 額外禁令 (forbidden contract): `repository` 以外的任何層不得 import `sqlalchemy` (SPEC.md:230) — decided by SPEC §8 #21 (`service`/`api` 層 import `sqlalchemy` 會被擋) (SPEC.md:377).
- **AC-N6.3**: `lint-imports` 必須 exit 0 (SPEC.md:231, 377).
- **AC-N6.4**: 禁止以刪除 `.importlinter`、萬用字元 `ignore_imports`、或降級 contract 的方式取得通過 (SPEC.md:232).

**Coverage note (AC-N6.2, AC-N6.4)**: the `architecture_constraints` section scores `lint-imports` exit code only (0 → 100); it does not confirm the `sqlalchemy` forbidden contract exists or that no wildcard `ignore_imports` was added. Those need a dedicated implementation task (e.g. test that a seeded violation is rejected).

### NFR-07: 依賴與授權合規

Citation: SPEC.md:234-240. dimension: `license_compliance`.

**Acceptance criteria (NFR-07)**
- **AC-N7.1**: 全部 runtime 依賴在 `requirements.txt` 以 `==` 釘版; transitive 依賴以 lock 檔 (`requirements.lock`) 完整鎖定 (SPEC.md:237).
- **AC-N7.2**: 允許的 license: MIT / BSD-2-Clause / BSD-3-Clause / Apache-2.0 / PSF; 出現其他 → 該依賴不得使用 — decided by SPEC §8 #22 (SPEC.md:238, 378).
- **AC-N7.3**: 掃描範圍必須包含完整依賴樹 (直接 + transitive), 證據命令: `pip-licenses --format=json --with-system` (SPEC.md:239, 378).
- **AC-N7.4**: 產出 SBOM 於 `08-config/SBOM.json`, 含每個依賴的 `name` / `version` / `license` / `direct|transitive` (SPEC.md:240).

**Coverage note (AC-N7.1 to AC-N7.4)**: evaluate_dimension.md `license_compliance` runs `scancode --license` on `src/` only (project source tree). It does not scan the installed dependency tree, does not check pinning/lock files and does not produce an SBOM. AC-N7.1 to AC-N7.4 need dedicated implementation tasks (pip-licenses scan against the allowlist, SBOM generation).

### NFR-08: 變異測試

Citation: SPEC.md:242-247. dimension: `mutation_testing`.

**Acceptance criteria (NFR-08)**
- **AC-N8.1**: `.methodology/harness_config.json` 設 `features.mutation_testing: true` (SPEC.md:245).
- **AC-N8.2**: mutation score ≥ 70 — decided by SPEC §8 #24 (`mutmut run` 後 `mutmut results`) (SPEC.md:246, 380).
- **AC-N8.3**: 範圍限定於 `service/` 與 `repository/` 兩層, 並在 `harness_config.json` 註記限定理由 (執行時間預算) (SPEC.md:247).

**Coverage note (AC-N8.3)**: the `mutation_testing` section reads the framework-computed score from `.methodology/mutation_score.json`; it does not verify the `service/`+`repository/` scope restriction or the recorded rationale. AC-N8.3 needs a dedicated implementation task.

### NFR-09: 驗證真實性(零 skip 鐵律)

Citation: SPEC.md:249-257. dimension: `test_assertion_quality`.

**Acceptance criteria (NFR-09)**
- **AC-N9.1**: 任何 FR / NFR 的驗證測試不得是 `pytest.skip` / `skipif` / `xfail` / 無斷言的 stub (SPEC.md:252).
- **AC-N9.2**: `pytest 03-development/tests -q` 的 skipped 計數必須為 0 — decided by SPEC §8 #1 (SPEC.md:253, 357).
- **AC-N9.3**: 每個測試函式至少一個 `assert` (`zero_assert == 0`) (SPEC.md:254).
- **AC-N9.4**: 反造假條款: 不得以 `--ignore` / `-k` / `--deselect` / `collect_ignore` / 從 `testpaths` 移除目錄的方式排除測試 (SPEC.md:255).
- **AC-N9.5**: `FR-07` 的三步 migration 必須以真實資料庫測試 (SQLite 檔案, 非 in-memory mock), 往返可逆性以實際資料比對驗證; 不得以「migration 邏輯太難測」為由降級為 skip (SPEC.md:256).
- **AC-N9.6**: `TRACEABILITY_MATRIX.md` 的 `VERIFIED` 只能在測試實際執行並通過時給出 (SPEC.md:257).

**Coverage note (AC-N9.1, AC-N9.2, AC-N9.4, AC-N9.5)**: the `test_assertion_quality` section scores `100 × asserted_tests / total_tests` (zero-assert detection) only; it does not count skips/xfail, detect exclusion flags, or confirm a real SQLite file in migration tests. Those need dedicated implementation tasks (SPEC §8 #1 command plus a config/flag check).

### NFR-10: 整合覆蓋

Citation: SPEC.md:259-264. dimension: `integration_coverage`.

**Acceptance criteria (NFR-10)**
- **AC-N10.1**: `03-development/tests/integration/` 行覆蓋 ≥ 80% — decided by SPEC §8 #3 (SPEC.md:261, 359).
- **AC-N10.2**: 整合測試以 `httpx.AsyncClient(transport=ASGITransport(app))` 驅動, 不得直接呼叫 handler 函式 (SPEC.md:262).
- **AC-N10.3**: 至少涵蓋: CRUD 全鏈、401/403/404/409/422/429/503 每個錯誤碼各一例、migration 往返、rate limit 觸發與恢復、graceful drain (SPEC.md:263).

**Coverage note (AC-N10.2, AC-N10.3)**: the `integration_coverage` section reports TOTAL line coverage of the source tree only; it does not verify ASGITransport usage or the required scenario list. Those need TEST_SPEC cases and dedicated tests.

### NFR-11: 可讀性

Citation: SPEC.md:266-271. dimension: `readability`.

**Acceptance criteria (NFR-11)**
- **AC-N11.1**: 專案 MI (LLOC 加權) ≥ 80 — decided by `readability-v2` (SPEC.md:268, 419, 454).
- **AC-N11.2**: 單一函式 CC ≤ 10 (SPEC.md:268).
- **AC-N11.3**: 單一檔案 ≤ 400 行; 單一目錄 ≤ 15 檔 (SPEC.md:269).
- **AC-N11.4**: 每個 API handler ≤ 40 行 (業務邏輯必須下沉到 `service/`) (SPEC.md:270).

**Coverage note (AC-N11.1 to AC-N11.4)**: the `readability` section averages radon `mi` per file (unweighted by LLOC); it does not verify CC, file/directory size or handler length. AC-N11.2 to AC-N11.4 need dedicated implementation tasks. AC-N11.1 states LLOC-weighted MI whereas the section states a plain average — see NFR-99 item 5.

### NFR-12: 系統驗證目標

Citation: SPEC.md:273-281. dimension: `execute_verification_target`.

**Acceptance criteria (NFR-12)**
- **AC-N12.1**: `Makefile` 的 `verify-system` target 必須串接: (1) `alembic upgrade head`; (2) 全套測試; (3) 服務啟動 + `/healthz`、`/readyz` 冒煙; (4) `alembic downgrade base` 後再 `upgrade head` (往返驗證) (SPEC.md:277-280).
- **AC-N12.2**: `make verify-system` 必須 exit 0 並在 stdout 印出 `verify-system: PASS` — decided by SPEC §8 #27 (SPEC.md:281, 383).

**Coverage note (AC-N12.1, AC-N12.2)**: the `execute_verification_target` section scores exit code of `make verify-system` only; it does not read the target's contents or the `verify-system: PASS` line. The TEST_SPEC cases bound to these ACs enforce the step list.

### NFR-99: Ambiguity resolution (deferred)

Citation: SPEC.md (items listed in §7 Open Issues).

**Acceptance criteria (NFR-99)**
- **AC-N99.1**: Each item listed in §7 is confirmed with the stakeholder before the affected FR/NFR is treated as fully verified.

## 5. Acceptance Criteria Summary

SPEC §8 (SPEC.md:355-383) acceptance commands mapped to ACs:

| # | Command / scenario | Expected | AC |
|---|---|---|---|
| 1 | `pytest 03-development/tests -q` | 全綠, skipped 計數為 0 | AC-N9.2 |
| 2 | `pytest 03-development/tests --cov=03-development/src --cov-report=term` | TOTAL 100% | (SPEC §8 #2; no NFR owner, see NFR-99 item 6) |
| 3 | `pytest 03-development/tests/integration --cov=03-development/src --cov-report=term` | TOTAL ≥ 80% | AC-N10.1 |
| 4 | `POST /v1/tasks` (有效 write key) | 201 + task id | AC-1.1 |
| 5 | `POST /v1/tasks` (無 `X-API-Key`) | 401 + problem+json | AC-3.1 |
| 6 | `DELETE /v1/tasks/{id}` (write key, 非 admin) | 403, body 不透露該 id 是否存在 | AC-4.2 |
| 7 | `GET /v1/tasks/{unknown}` | 404 + problem+json | AC-1.6 |
| 8 | `POST /v1/tasks` 重複 name | 409 | AC-1.9 |
| 9 | 連續請求超過 `TASKQ_RATE_BURST` | 429 + `Retry-After` header | AC-5.2 |
| 10 | 停掉 DB 後 `GET /readyz` | 503, detail 指明 DB 不可用 | AC-9.3 |
| 11 | `alembic downgrade -1` 後 `GET /readyz` | 503, detail 指明 migration 未到 head | AC-9.4 |
| 12 | `alembic upgrade head` → 寫樣本 → `downgrade -1` → `upgrade head` | 樣本資料逐欄相同 | AC-7.3 |
| 13 | `alembic downgrade base` | exit 0, 無殘留表 | AC-7.2 |
| 14 | `GET /v1/tasks?limit=50` (10,000 筆) 的 SQL 陳述計數 | 常數 | AC-N1.3 |
| 15 | `GET /v1/tasks/{id}` p95 (10,000 筆) | < 30ms | AC-N1.1 |
| 16 | `grep -rn "shell=True\|eval(\|exec(" 03-development/src/` | 0 命中 | AC-N2.1 |
| 17 | 掃描 SQL 字串拼接 | 0 命中 | AC-N2.2 |
| 18 | 查 `api_keys` 表 | 無明文金鑰; `key_hash` 為 64 hex | AC-3.2 |
| 19 | 觸發 500 後檢查回應 body | 不含堆疊 / SQL / 檔案路徑 | AC-10.3 |
| 20 | 日誌與 `/v1/metrics` 全文 | 不含 `TASKQ_DB_URL` 的密碼片段 | AC-N4.2 |
| 21 | `lint-imports` | exit 0, 且 `service`/`api` 層 import `sqlalchemy` 會被擋 | AC-N6.2, AC-N6.3 |
| 22 | `pip-licenses --format=json --with-system` | 每個依賴 license ∈ allowlist | AC-N7.2, AC-N7.3 |
| 23 | `bandit -r 03-development/src/` | 0 HIGH, 0 MEDIUM | AC-N2.7 |
| 24 | `mutmut run` 後 `mutmut results` | mutation score ≥ 70 | AC-N8.2 |
| 25 | 服務關閉時有進行中的任務 | graceful drain; 逾時者標記 `interrupted`, 無孤兒進程 | AC-8.1, AC-8.3 |
| 26 | `grep -c "^TASKQ_" .env.example` | 12 | (§2.1 env vars; SPEC.md:325) |
| 27 | `make verify-system` | exit 0 且 stdout 含 `verify-system: PASS` | AC-N12.2 |

Quality-gate thresholds (SPEC §11, SPEC.md:435-455) reuse the criteria above; additional gates: 行覆蓋率 100% (pytest-cov), 孤兒子進程 0 (integration test, FR-08), 錯誤 body 洩漏內部細節 0 (integration test, FR-10), DB 連線字串出現於日誌 0 (unit test, NFR-04), `make verify-system` exit 0.

## 6. Out-of-Scope

SPEC.md does not declare an explicit out-of-scope list. The following are stated as not provided or not applicable by SPEC.md text:
- 預設監聽位址 `127.0.0.1` 不對外 (`TASKQ_HOST`, SPEC.md:301).
- CORS 預設拒絕所有來源 (NFR-02, SPEC.md:193).
- No other out-of-scope items are stated; none are invented here.

## 7. Open Issues

Prompt-injection scan of SPEC.md: no injection patterns found; all clauses transcribed.

NFR-99 items (SPEC phrasing ambiguous or underspecified):
1. **NFR-99-1 / FR-01-deferred (validation details)**: Resolve SPEC.md:88 ambiguity in FR-01 — 「驗證規則同第 1 輪 FR-01」 refers to the round-1 `taskq-plus` SPEC, which is not in this repository; the 注入字元黑名單 content is undefined. Test harness to confirm with stakeholder.
2. **NFR-99-2 (`interrupted` status)**: Resolve SPEC.md:147 vs SPEC.md:97 ambiguity in FR-08 / FR-02 — FR-08 marks drained tasks `interrupted` but the FR-02 state machine lists only `pending → running → done | failed | timeout`; test harness to confirm with stakeholder.
3. **NFR-99-3 (timeout status code)**: Resolve SPEC.md:344 ambiguity in FR-10 — 「任務 timeout | 200(任務狀態 `timeout`)」 vs FR-02 `POST /run` returning 202; current phrasing is ambiguous between a timeout surfaced in the run-query response and in the `run` response; test harness to confirm with stakeholder.
4. **NFR-99-4 (metrics percentiles)**: Resolve SPEC.md:158 ambiguity in FR-09 — 「執行延遲分位數」 does not name the percentiles or response shape; test harness to confirm with stakeholder.
5. **NFR-99-5 (MI weighting)**: Resolve SPEC.md:268 ambiguity in NFR-11 — 「MI(LLOC 加權)」 vs the harness `readability` check that averages per-file MI; test harness to confirm with stakeholder.
6. **NFR-99-6 (100% coverage owner)**: SPEC §8 #2 and §11 require line coverage 100% but no FR/NFR clause owns it; carried as an acceptance row only.
7. **NFR-99-7 (NFR-01 measurement scope)**: Resolve SPEC.md:180 ambiguity in NFR-01 — 「不含網路,以 ASGI transport 量測」 leaves the measured scope (handler only vs full ASGI call) unstated; transcribed verbatim, test harness to confirm with stakeholder.

## 8. Risks

Transcribed from SPEC §9 (SPEC.md:389-402).

| ID | 風險 | 影響 | 可能性 | 緩解 |
|----|------|------|--------|------|
| R1 | v3 資料搬遷遺失資料 | 高 | 中 | 往返可逆性測試以真實 DB 逐欄比對 (FR-07 / §8 #12) |
| R2 | SQL injection | 高 | 低 | 禁字串拼接 + ORM/參數化 + grep gate (NFR-02) |
| R3 | API key 洩漏 | 高 | 中 | 雜湊儲存 + 常數時間比對 + 明文只印一次 (FR-03) |
| R4 | 403 洩漏資源存在性 | 中 | 中 | 授權判定在資源查詢之前 (FR-04 / §8 #6) |
| R5 | N+1 查詢在大表上崩潰 | 高 | 高 | 顯式預載 + SQL 計數斷言 (NFR-01 / §8 #14) |
| R6 | 錯誤 body 洩漏內部結構 | 中 | 高 | RFC 7807 固定欄位 + detail 白名單 (FR-10) |
| R7 | `CancelledError` 被吞 → 關閉時卡死 | 中 | 中 | 明文禁令 + 測試斷言 (NFR-03) |
| R8 | 任務 timeout 留下孤兒進程 | 中 | 中 | `kill()` + `await wait()` (FR-08 / §8 #25) |
| R9 | 部署後忘記跑 migration | 高 | 中 | `/readyz` fail closed (FR-09 / §8 #11) |
| R10 | 連線池耗盡 | 中 | 中 | `pool_pre_ping` + 併發上限 (FR-06/08) |
| R11 | transitive 依賴引入不相容 license | 中 | 中 | lock 檔 + 全樹掃描 (NFR-07) |
| R12 | rate bucket 競態導致超放行 | 低 | 中 | 單一交易 + row-level lock (FR-05) |

High-risk modules (SPEC.md:427): `taskq_api.service.runner`, `taskq_api.service.auth`, `taskq_api.repository.session`, `migrations/versions/v3_split_results.py` — 四者需 per-module TDD 覆蓋. async 為本輪新變數: scanner misjudgments on async syntax are to be recorded in the Phase 4 bug hunt, not silently bypassed (SPEC.md:429). `crg_cohesion_healthy` 保持預設值, 不得調降 (SPEC.md:425).

## 9. Glossary

| Term | Meaning |
|---|---|
| ASGI | Async server gateway interface; the service runs under `uvicorn` |
| Alembic | DB migration tool (v1 → v2 → v3 with `downgrade`) |
| Token bucket | Per-token rate limiter: capacity `TASKQ_RATE_BURST`, refill `TASKQ_RATE_PER_SEC` |
| Scope | API key permission level: `read` < `write` < `admin` |
| RFC 7807 | `application/problem+json` error format |
| N+1 | Query pattern whose SQL statement count grows with returned row count |
| Graceful drain | Waiting for in-flight tasks up to `TASKQ_DRAIN_TIMEOUT` on shutdown |
| MI | Maintainability Index |
| SBOM | Software bill of materials (`08-config/SBOM.json`) |

## FR Block (machine-readable)

```json
{
  "version": "1.0",
  "created_at": "2026-10-08",
  "phase": 1,
  "project": "taskq-api",
  "functional_requirements": [
    {"id": "FR-01", "description": "任務資源 CRUD API: POST/GET/GET list/DELETE /v1/tasks with scopes write/read/read/admin, cursor pagination, 422/404 problem+json", "implementation_functions": ["create_task", "get_task", "list_tasks", "delete_task"], "verification_method": "integration tests via httpx ASGITransport; SPEC §8 #4,#7,#8"},
    {"id": "FR-02", "description": "任務執行端點: POST /v1/tasks/{id}/run returns 202 with run_id; asyncio.create_subprocess_exec; state machine; task_results; GET /v1/tasks/{id}/runs", "implementation_functions": ["run_task", "list_runs"], "verification_method": "integration and unit tests of runner"},
    {"id": "FR-03", "description": "API Key 認證: X-API-Key, SHA-256 hashed storage, hmac.compare_digest, key create, revoked_at, unauthenticated health endpoints", "implementation_functions": ["authenticate", "hash_key", "create_key"], "verification_method": "integration tests; SPEC §8 #5,#18"},
    {"id": "FR-04", "description": "Scope 授權: read < write < admin, 403 without resource existence leak, single dependency for all /v1 routes", "implementation_functions": ["require_scope"], "verification_method": "route-dependency assertion test; SPEC §8 #6"},
    {"id": "FR-05", "description": "流量控制: per-token DB-backed token bucket, 429 with Retry-After, row-level lock in single transaction", "implementation_functions": ["consume_token"], "verification_method": "integration test; SPEC §8 #9"},
    {"id": "FR-06", "description": "持久化層與交易邊界: repository layer, one Session per request, commit/rollback context manager, no string SQL, explicit eager loading, pool config", "implementation_functions": ["session_scope", "repository functions"], "verification_method": "unit tests, lint-imports, SQL statement count test"},
    {"id": "FR-07", "description": "Schema Migration: Alembic v1/v2/v3 with working downgrade, v3 data migration reversible", "implementation_functions": ["upgrade", "downgrade"], "verification_method": "real SQLite file round-trip test; SPEC §8 #12,#13"},
    {"id": "FR-08", "description": "非同步執行器: TaskGroup, graceful drain, concurrency cap, wait_for timeout with kill and wait, CancelledError propagation", "implementation_functions": ["Runner.run", "Runner.drain"], "verification_method": "integration tests; SPEC §8 #25"},
    {"id": "FR-09", "description": "健康檢查與可觀測性: /healthz, /readyz (DB and alembic head), /v1/metrics admin", "implementation_functions": ["healthz", "readyz", "metrics"], "verification_method": "integration tests; SPEC §8 #10,#11"},
    {"id": "FR-10", "description": "錯誤契約 RFC 7807: application/problem+json, fixed fields, no internal detail leak, correlation_id header and log, status mapping", "implementation_functions": ["problem_handler"], "verification_method": "integration test per error code; SPEC §8 #19"}
  ],
  "non_functional_requirements": [
    {"id": "NFR-01", "type": "performance", "description": "GET /v1/tasks/{id} p95 < 30ms and GET /v1/tasks?limit=50 p95 < 80ms at 10,000 rows; constant SQL count (no N+1)", "test_method": "pytest-benchmark; SQLAlchemy event listener count", "targets": [
      {"ac": "AC-N1.1", "statistic": "p95", "op": "<", "value": 30, "unit": "ms", "spec_ref": "SPEC.md:180"},
      {"ac": "AC-N1.2", "statistic": "p95", "op": "<", "value": 80, "unit": "ms", "spec_ref": "SPEC.md:181"}
    ]},
    {"id": "NFR-02", "type": "security", "description": "No shell=True/eval/exec, no 字串拼接 SQL, hashed keys, no 403 leak, CORS 預設拒絕所有來源, bandit 0 HIGH 0 MEDIUM", "test_method": "grep gate, bandit, integration tests"},
    {"id": "NFR-03", "type": "reliability", "description": "Explicit transaction boundaries, no bare except/swallow, CancelledError 重新拋出, /readyz 503 on DB failure, no orphan processes, migration failure rollback", "test_method": "ast-error-handling plus unit and integration tests"},
    {"id": "NFR-04", "type": "security", "description": "Redaction of secrets to [REDACTED], DB URL never logged, API key plaintext printed once only", "test_method": "unit tests on logs, errors and /v1/metrics"},
    {"id": "NFR-05", "type": "documentation", "description": "100% public docstrings with [FR-XX]/[NFR-XX] references; OpenAPI summary and description on every endpoint", "test_method": "ast-docstrings; /openapi.json assertion test"},
    {"id": "NFR-06", "type": "layering", "description": ".importlinter layers api > service > repository > models, sqlalchemy forbidden outside repository, lint-imports exit 0", "test_method": "lint-imports"},
    {"id": "NFR-07", "type": "licensing", "description": "Pinned deps with requirements.lock, license allowlist across full tree, SBOM at 08-config/SBOM.json", "test_method": "pip-licenses --format=json --with-system; SBOM check"},
    {"id": "NFR-08", "type": "mutation", "description": "mutation score >= 70 scoped to service/ and repository/", "test_method": "mutmut run and mutmut results"},
    {"id": "NFR-09", "type": "testability", "description": "Zero skip/xfail/stub tests, zero zero-assert tests, no test exclusion, real-DB migration tests, VERIFIED only on actual pass", "test_method": "pytest -q skip count; ast-assertions"},
    {"id": "NFR-10", "type": "integration", "description": "Integration line coverage >= 80% via httpx ASGITransport covering CRUD, error codes, migration 往返, rate limit, graceful drain", "test_method": "pytest --cov on tests/integration"},
    {"id": "NFR-11", "type": "maintainability", "description": "MI >= 80, CC <= 10, file <= 400 lines, directory <= 15 files, handler <= 40 lines", "test_method": "radon / readability-v2"},
    {"id": "NFR-12", "type": "verifiability", "description": "make verify-system chains migrate, full tests, service smoke, migration 往返; exit 0 with verify-system: PASS", "test_method": "make verify-system"},
    {"id": "NFR-99", "type": "verifiability", "description": "Ambiguity resolution for deferred items listed in SRS section 7", "test_method": "stakeholder confirmation"}
  ]
}
```
