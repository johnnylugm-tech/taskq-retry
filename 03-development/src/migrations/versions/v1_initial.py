"""[FR-07] v1: create tasks and api_keys.

Citations: SPEC.md:130-143.
"""
import sqlalchemy as sa
from alembic import op

revision = "v1"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("command", sa.String(1000), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("result_json", sa.Text, nullable=True),
    )
    op.create_table(
        "api_keys",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("revoked_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("api_keys")
    op.drop_table("tasks")
