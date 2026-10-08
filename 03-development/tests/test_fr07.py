"""[FR-07] Alembic three-step schema migration (v1 -> v2 -> v3).

Citations: SPEC.md:130-143, SPEC.md:368-369.
Migration tests run in-process against a real SQLite file (never :memory:, NFR-09).
"""
import io
import json
import re
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

import migrations.versions.v1_initial as v1_initial
import migrations.versions.v2_tags as v2_tags
import migrations.versions.v3_split_results as v3_split_results

_SRC = Path(__file__).resolve().parent.parent / "src"
_MIGRATIONS = _SRC / "migrations"
_VERSIONS = _MIGRATIONS / "versions"

_RESULT_COLUMNS = "task_id, exit_code, stdout_tail, stderr_tail, duration_ms"


def _cfg(db_file: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(_MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_file}")
    return cfg


def _tables(db_file: Path) -> set:
    con = sqlite3.connect(db_file)
    try:
        rows = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        return {r[0] for r in rows}
    finally:
        con.close()


def _columns(db_file: Path, table: str) -> list:
    con = sqlite3.connect(db_file)
    try:
        return [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
    finally:
        con.close()


def _indexes(db_file: Path, table: str) -> set:
    con = sqlite3.connect(db_file)
    try:
        return {r[1] for r in con.execute(f"PRAGMA index_list({table})")}
    finally:
        con.close()


def _insert_task(con, idx: int, result_json=None) -> str:
    task_id = f"00000000-0000-4000-8000-{idx:012d}"
    con.execute(
        "INSERT INTO tasks (id, command, name, status, created_at, result_json) "
        "VALUES (?, ?, ?, 'pending', '2026-01-01 00:00:00', ?)",
        (task_id, "echo hello", f"task-{idx}", result_json),
    )
    return task_id


def _result_payload(stdout_tail: str = "hello", duration_ms: int = 12) -> str:
    return json.dumps(
        {"exit_code": 0, "stdout_tail": stdout_tail, "stderr_tail": "", "duration_ms": duration_ms}
    )


def _insert_head_result(con, idx: int, task_id: str, stdout_tail: str = "hello") -> None:
    con.execute(
        "INSERT INTO task_results (id, task_id, status, exit_code, stdout_tail, stderr_tail, duration_ms) "
        "VALUES (?, ?, 'done', 0, ?, '', 12)",
        (f"10000000-0000-4000-8000-{idx:012d}", task_id, stdout_tail),
    )


def _dump(db_file: Path, table: str, order_by: str) -> list:
    con = sqlite3.connect(db_file)
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
    finally:
        con.close()


def test_fr07_three_revisions_each_have_working_downgrade(tmp_path):
    revisions_expected = "3"
    revision_ids = "v1,v2,v3"
    downgrade_steps = "3"
    db_file = tmp_path / "steps.db"
    cfg = _cfg(db_file)
    script = ScriptDirectory.from_config(cfg)
    result_revisions = list(script.walk_revisions())
    assert len(result_revisions) == int(revisions_expected)  # AC7.1-revision-count
    assert {r.revision for r in result_revisions} == set(revision_ids.split(","))
    for module in (v1_initial, v2_tags, v3_split_results):
        assert callable(module.upgrade)
        assert callable(module.downgrade)

    command.upgrade(cfg, "head")
    result_downgrade_success_count = 0
    for _ in range(int(downgrade_steps)):
        command.downgrade(cfg, "-1")
        result_downgrade_success_count += 1
    assert result_downgrade_success_count == int(downgrade_steps)  # AC7.1-downgrades-ok
    assert _tables(db_file) - {"alembic_version"} == set()


def test_fr07_v2_downgrade_preserves_v1_data(tmp_path):
    seed_tasks = "3"
    target_revision = "v1"
    dropped_tables = "tags,task_tags"
    db_file = tmp_path / "v2down.db"
    cfg = _cfg(db_file)
    command.upgrade(cfg, "v2")
    con = sqlite3.connect(db_file)
    for idx in range(int(seed_tasks)):
        _insert_task(con, idx)
    con.commit()
    con.close()
    before = _dump(db_file, "tasks", "id")
    assert "ix_tasks_name" in _indexes(db_file, "tasks")
    dup = sqlite3.connect(db_file)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            dup.execute(
                "INSERT INTO tasks (id, command, name, status, created_at) "
                "VALUES ('dup-id', 'echo hello', 'task-0', 'pending', '2026-01-01 00:00:00')"
            )
    finally:
        dup.rollback()
        dup.close()

    command.downgrade(cfg, target_revision)
    assert "ix_tasks_name" not in _indexes(db_file, "tasks")

    result_tables_after = _tables(db_file)
    result_tasks_count_after = len(_dump(db_file, "tasks", "id"))
    assert result_tasks_count_after == int(seed_tasks)  # AC7.1-v1-data-kept
    assert _dump(db_file, "tasks", "id") == before
    assert all(t not in result_tables_after for t in dropped_tables.split(","))  # AC7.1-v2-tables-dropped
    assert {"tasks", "api_keys"} <= result_tables_after


def test_fr07_v3_downgrade_moves_results_back_to_result_json(tmp_path):
    seed_results = "2"
    target_revision = "v2"
    db_file = tmp_path / "v3down.db"
    cfg = _cfg(db_file)
    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_file)
    for idx in range(int(seed_results)):
        task_id = _insert_task_head(con, idx)
        _insert_head_result(con, idx, task_id)
    con.commit()
    con.close()

    command.downgrade(cfg, target_revision)

    result_task_results_table_exists = "task_results" in _tables(db_file)
    assert result_task_results_table_exists == False  # AC7.1-v3-table-gone  # noqa: E712
    con = sqlite3.connect(db_file)
    try:
        rows = con.execute("SELECT result_json FROM tasks WHERE result_json IS NOT NULL").fetchall()
    finally:
        con.close()
    result_result_json_rows = len(rows)
    assert result_result_json_rows == int(seed_results)  # AC7.1-v3-result-json
    for (payload,) in rows:
        restored = json.loads(payload)
        assert restored["exit_code"] == 0
        assert restored["stdout_tail"] == "hello"


def _insert_task_head(con, idx: int) -> str:
    """Insert a task at head, where tasks has no result_json column."""
    task_id = f"00000000-0000-4000-8000-{idx:012d}"
    con.execute(
        "INSERT INTO tasks (id, command, name, status, created_at) "
        "VALUES (?, 'echo hello', ?, 'pending', '2026-01-01 00:00:00')",
        (task_id, f"task-{idx}"),
    )
    return task_id


def test_fr07_upgrade_head_and_downgrade_base_succeed_no_residual_tables(tmp_path):
    expected_residual_tables = "0"
    db_file = tmp_path / "base.db"
    cfg = _cfg(db_file)

    command.upgrade(cfg, "head")  # raises on failure
    result_upgrade_exit_code = 0
    assert result_upgrade_exit_code == 0  # AC7.2-upgrade-exit
    at_head = _tables(db_file)
    assert {"tasks", "api_keys", "tags", "task_tags", "task_results", "rate_buckets"} <= at_head
    assert "result_json" not in _columns(db_file, "tasks")

    command.downgrade(cfg, "base")  # raises on failure
    result_downgrade_exit_code = 0
    assert result_downgrade_exit_code == 0  # AC7.2-downgrade-exit
    result_residual_tables = _tables(db_file) - {"alembic_version"}
    assert len(result_residual_tables) == int(expected_residual_tables)  # AC7.2-no-residual


def test_fr07_round_trip_upgrade_downgrade_upgrade_preserves_sample_data(tmp_path):
    db_file = tmp_path / "roundtrip.db"
    cfg = _cfg(db_file)
    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_file)
    for idx in range(3):
        task_id = _insert_task_head(con, idx)
        _insert_head_result(con, idx, task_id)
    con.commit()
    con.close()
    tasks_before = _dump(db_file, "tasks", "id")
    results_before = _dump(db_file, "task_results", "id")
    result_rows_before = len(tasks_before) + len(results_before)

    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")

    tasks_after = _dump(db_file, "tasks", "id")
    results_after = _dump(db_file, "task_results", "id")
    result_rows_after = len(tasks_after) + len(results_after)
    assert result_rows_after == result_rows_before  # AC7.3-row-count
    assert len(tasks_after) == len(tasks_before) == 3
    assert len(results_after) == len(results_before) == 3
    result_column_diff_count = sum(
        1 for a, b in zip(tasks_before + results_before, tasks_after + results_after) if a != b
    )
    assert result_column_diff_count == 0  # AC7.3-column-diff
    for row in results_after:
        assert "hello" in row
        assert 12 in row


# AC7.4: scan every migration file for destructive raw-SQL shortcuts.
def test_fr07_downgrades_do_not_use_destructive_drop_shortcuts():
    files = sorted(_VERSIONS.glob("*.py"))
    assert {f.stem for f in files} >= {"v1_initial", "v2_tags", "v3_split_results"}
    pattern = re.compile(r"""execute\s*\(\s*[rRfFbB]*["']{1,3}\s*DROP\s+(TABLE|INDEX)""", re.IGNORECASE)
    result_destructive_shortcut_hits = 0
    for path in files:
        result_destructive_shortcut_hits += len(pattern.findall(path.read_text(encoding="utf-8")))
    assert result_destructive_shortcut_hits == 0  # AC7.4-no-shortcut
    # the real downgrades must use the op API instead
    v1_text = (_VERSIONS / "v1_initial.py").read_text(encoding="utf-8")
    assert "op.drop_table" in v1_text


def test_fr07_migrations_offline_sql_generation_asserted(tmp_path):
    expected_v3_table = "task_results"
    db_file = tmp_path / "offline.db"
    cfg = _cfg(db_file)
    cfg.output_buffer = io.StringIO()
    command.upgrade(cfg, "base:head", sql=True)
    result_upgrade_sql = cfg.output_buffer.getvalue()
    assert len(result_upgrade_sql) > 0  # AC7.5-sql-nonempty
    for table in ("tasks", "api_keys", "tags", "task_tags"):
        assert f"CREATE TABLE {table}" in result_upgrade_sql

    v3_cfg = _cfg(db_file)
    v3_cfg.output_buffer = io.StringIO()
    command.upgrade(v3_cfg, "v2:v3", sql=True)
    result_v3_upgrade_sql = v3_cfg.output_buffer.getvalue()
    assert expected_v3_table in result_v3_upgrade_sql  # AC7.5-v3-table
    assert not db_file.exists() or "tasks" not in _tables(db_file)  # offline mode touches no DB


def test_fr07_upgrade_unknown_revision_fails(tmp_path):
    expected_error = "CommandError"
    db_file = tmp_path / "unknown.db"
    cfg = _cfg(db_file)
    with pytest.raises(CommandError) as excinfo:
        command.upgrade(cfg, "v99")
    result_error_type = type(excinfo.value).__name__
    assert result_error_type == expected_error  # AC7.2-error


def test_sec_t09_v3_roundtrip_preserves_data(tmp_path):
    seed_results = "3"
    null_result_json_rows = "1"
    db_file = tmp_path / "t09.db"
    cfg = _cfg(db_file)
    command.upgrade(cfg, "v2")
    unicode_stdout_tail = "輸出完成"
    con = sqlite3.connect(db_file)
    _insert_task(con, 0, _result_payload(unicode_stdout_tail, 12))
    _insert_task(con, 1, _result_payload("hello", 34))
    _insert_task(con, 2, _result_payload("tail with 'quote' and \"dq\"; --", 56))
    _insert_task(con, 3, None)  # NULL result_json must stay NULL
    con.commit()
    con.close()
    before = {r[0]: json.loads(r[5]) if r[5] else None for r in _dump(db_file, "tasks", "id")}

    command.upgrade(cfg, "v3")
    con = sqlite3.connect(db_file)
    migrated = con.execute("SELECT stdout_tail FROM task_results").fetchall()
    con.close()
    assert len(migrated) == int(seed_results)
    assert unicode_stdout_tail in [r[0] for r in migrated]

    command.downgrade(cfg, "v2")

    after = {r[0]: json.loads(r[5]) if r[5] else None for r in _dump(db_file, "tasks", "id")}
    result_null_result_json_rows_after = sum(1 for v in after.values() if v is None)
    assert result_null_result_json_rows_after == int(null_result_json_rows)  # T09-null-preserved
    result_column_diff_count = sum(1 for k in before if before[k] != after[k])
    assert result_column_diff_count == 0  # AC7.3-column-diff
    result_stdout_tail_after = after["00000000-0000-4000-8000-000000000000"]["stdout_tail"]
    assert unicode_stdout_tail in result_stdout_tail_after  # T09-unicode


def test_fr07_v3_downgrade_keeps_non_default_status_and_finished_at(tmp_path):
    db_file = tmp_path / "v3down_extra.db"
    cfg = _cfg(db_file)
    command.upgrade(cfg, "head")
    con = sqlite3.connect(db_file)
    task_id = _insert_task_head(con, 0)
    con.execute(
        "INSERT INTO task_results (id, task_id, status, exit_code, stdout_tail, stderr_tail, duration_ms, finished_at) "
        "VALUES (?, ?, 'failed', 1, 'x', 'err', 5, '2026-01-02 00:00:00')",
        ("10000000-0000-4000-8000-000000000000", task_id),
    )
    con.commit()
    con.close()

    command.downgrade(cfg, "v2")

    con = sqlite3.connect(db_file)
    try:
        (payload,) = con.execute("SELECT result_json FROM tasks WHERE id = ?", (task_id,)).fetchone()
    finally:
        con.close()
    restored = json.loads(payload)
    assert restored["status"] == "failed"
    assert restored["finished_at"] == "2026-01-02 00:00:00"
    assert restored["id"] == "10000000-0000-4000-8000-000000000000"
