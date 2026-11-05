# migrations/versions/0001_initial.py
"""Начальная схема собственных jobs, events, outbox, sessions и rate_limits."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Создаёт только собственные таблицы; shared PostgreSQL/broker не затрагиваются."""
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("owner", sa.String(160), nullable=False),
        sa.Column("idempotency_key", sa.String(128)),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("owner", "idempotency_key"),
    )
    op.create_index("ix_jobs_owner", "jobs", ["owner"])
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_table(
        "events",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("data", sa.JSON(), nullable=False),
    )
    op.create_table(
        "outbox",
        sa.Column("job_id", sa.String(36), primary_key=True),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_outbox_next_at", "outbox", ["next_at"])
    op.create_table(
        "sessions",
        sa.Column("owner", sa.String(160), primary_key=True),
        sa.Column("prompts", sa.JSON()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])
    op.create_table(
        "rate_limits",
        sa.Column("owner", sa.String(160), primary_key=True),
        sa.Column("window", sa.Integer(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    """Удаляет собственную схему только по явной команде администратора Alembic downgrade."""
    for name in ("rate_limits", "sessions", "outbox", "events", "jobs"):
        op.drop_table(name)
