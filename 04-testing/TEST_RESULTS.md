# TEST_RESULTS — taskq-retry (Phase 4)

Run date: 2026-11-03
Scope: `03-development/tests` (test_target from `.sessi-work/phase4_ctx.json`), not the repository root.
Command: `.venv/bin/python -m pytest /Users/johnny/projects/taskq-retry/03-development/tests --cov=03-development/src --cov-report=term-missing -q`
Raw output: `04-testing/coverage_raw.txt`

## Pytest summary line (verbatim)

```
281 passed, 2 warnings in 19.80s
```

## Results

| Metric | Count |
|--------|-------|
| Cases run | 281 |
| Passed | 281 |
| Failed | 0 |
| Skipped | 0 |
| Warnings | 2 |

Benchmarks (pytest-benchmark, NFR-01): `test_nfr01_get_task_p95_under_30ms_at_10k_rows` mean 1.88 ms; `test_nfr01_list_tasks_limit_50_p95_under_80ms_at_10k_rows` mean 3.11 ms.

## Deferred issues

No failing or skipped tests. Two warnings, neither deferred as a defect:
1. `StarletteDeprecationWarning` from `fastapi/testclient.py` (third-party, httpx vs httpx2).
2. `SAWarning` in `test_fr09.py::test_sec_t16_db_url_password_absent_from_logs` (the test intentionally passes a `password` query arg to a pysqlite URL, which ignores it).
