# migrations/versions/0002_adjacent_ocr.py
"""Отделяет новый алгоритм от старых заданий, сохраняя готовые результаты и файлы."""

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Завершает активные задания прежней версии диагностикой, не меняя их реквизиты."""
    connection = op.get_bind()
    jobs = sa.table(
        "jobs",
        sa.column("id"),
        sa.column("status"),
        sa.column("lease_until"),
        sa.column("data", sa.JSON()),
    )
    events = sa.table(
        "events",
        sa.column("job_id"),
        sa.column("sequence"),
        sa.column("kind"),
        sa.column("data", sa.JSON()),
    )
    outbox = sa.table("outbox", sa.column("job_id"))
    now = datetime.now(UTC)
    for row in connection.execute(sa.select(jobs.c.id, jobs.c.data)).all():
        data = dict(row.data)
        if data.get("algorithm_version"):
            continue
        data["algorithm_version"] = "retired-window-v3"
        data["comparisons"] = []
        proposal = data.get("proposed_plan")
        if proposal:
            data["proposed_plan"] = {
                "documents": [
                    {"client_id": f"legacy-{i}", "pages": d["pages"]}
                    for i, d in enumerate(proposal["documents"], 1)
                ],
                "excluded_pages": [],
            }
        if data.get("segmentation_source") == "page_fallback":
            data["segmentation_source"] = "manual"
        if data["status"] not in {"completed", "completed_with_warnings", "failed", "cancelled"}:
            data.update(
                status="failed",
                stage="failed",
                completed_at=now.isoformat(),
                expires_at=(now + timedelta(hours=1)).isoformat(),
                error={
                    "code": "algorithm_version_changed",
                    "message": "Алгоритм разделения обновлён; загрузите исходники новым пакетом",
                },
                fence=data.get("fence", 0) + 1,
                cursor=data.get("cursor", 0) + 1,
            )
            connection.execute(sa.delete(outbox).where(outbox.c.job_id == row.id))
            connection.execute(
                sa.insert(events).values(
                    job_id=row.id,
                    sequence=data["cursor"],
                    kind="terminal",
                    data={"status": "failed", "error": data["error"]},
                )
            )
        connection.execute(
            sa.update(jobs)
            .where(jobs.c.id == row.id)
            .values(data=data, status=data["status"], lease_until=None)
        )


def downgrade() -> None:
    """Не возобновляет устаревшие задания: их старые ответы не совместимы с новым промптом."""
