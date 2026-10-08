"""[FR-07] v3: split tasks.result_json into task_results, migrating data both ways.

Citations: SPEC.md:130-143.
"""
import json
import uuid
from typing import Any

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.engine import Connection

revision = "v3"
down_revision = "v2"
branch_labels = None
depends_on = None

_NS = uuid.UUID("6f1d3c52-8a0e-4b7a-9c11-2f5e7d9a4b30")
_DEFAULT_STATUS = "done"
_FIELDS = ("exit_code", "stdout_tail", "stderr_tail", "duration_ms")


def _derived_id(task_id: str) -> str:
    return str(uuid.uuid5(_NS, task_id))


def _move_json_to_results(bind: Connection, results: sa.Table) -> None:
    rows = bind.execute(sa.text("SELECT id, result_json FROM tasks WHERE result_json IS NOT NULL")).fetchall()
    for row in rows:
        task_id = str(row[0])
        data = json.loads(str(row[1]))
        bind.execute(
            sa.insert(results).values(
                id=data.get("id", _derived_id(task_id)),
                task_id=task_id,
                status=data.get("status", _DEFAULT_STATUS),
                finished_at=data.get("finished_at"),
                **{f: data.get(f) for f in _FIELDS},
            )
        )


def _move_results_to_json(bind: Connection) -> None:
    rows = bind.execute(
        sa.text(
            "SELECT id, task_id, status, exit_code, stdout_tail, stderr_tail, duration_ms, finished_at "
            "FROM task_results ORDER BY finished_at, id"
        )
    ).fetchall()
    for rid, raw_task_id, status, *rest in rows:
        task_id = str(raw_task_id)
        *fields, finished_at = rest
        data: dict[str, Any] = dict(zip(_FIELDS, fields))
        if rid != _derived_id(task_id):
            data["id"] = rid
        if status != _DEFAULT_STATUS:
            data["status"] = status
        if finished_at is not None:
            data["finished_at"] = str(finished_at)
        bind.execute(
            sa.text("UPDATE tasks SET result_json = :p WHERE id = :t"),
            {"p": json.dumps(data, ensure_ascii=False), "t": task_id},
        )


def upgrade() -> None:
    results = op.create_table(
        "task_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("exit_code", sa.Integer),
        sa.Column("stdout_tail", sa.Text),
        sa.Column("stderr_tail", sa.Text),
        sa.Column("duration_ms", sa.Integer),
        sa.Column("finished_at", sa.DateTime),
    )
    op.create_index("ix_task_results_task_id", "task_results", ["task_id"])
    if not context.is_offline_mode():
        _move_json_to_results(op.get_bind(), results)
    op.drop_column("tasks", "result_json")


def downgrade() -> None:
    op.add_column("tasks", sa.Column("result_json", sa.Text, nullable=True))
    if not context.is_offline_mode():
        _move_results_to_json(op.get_bind())
    op.drop_index("ix_task_results_task_id", table_name="task_results")
    op.drop_table("task_results")
