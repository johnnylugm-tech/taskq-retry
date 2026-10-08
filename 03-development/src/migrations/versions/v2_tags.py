"""[FR-07] v2: tags, task_tags, rate_buckets and the unique tasks.name index.

Citations: SPEC.md:130-143.
"""
import sqlalchemy as sa
from alembic import op

revision = "v2"
down_revision = "v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tags",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
    )
    op.create_table(
        "task_tags",
        sa.Column("task_id", sa.String(36), sa.ForeignKey("tasks.id"), primary_key=True),
        sa.Column("tag_id", sa.String(36), sa.ForeignKey("tags.id"), primary_key=True),
    )
    op.create_table(
        "rate_buckets",
        sa.Column("key_id", sa.String(36), sa.ForeignKey("api_keys.id"), primary_key=True),
        sa.Column("tokens", sa.Float, nullable=False),
        sa.Column("refilled_at", sa.Float, nullable=False),
    )
    op.create_index("ix_tasks_name", "tasks", ["name"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_tasks_name", table_name="tasks")
    op.drop_table("rate_buckets")
    op.drop_table("task_tags")
    op.drop_table("tags")
