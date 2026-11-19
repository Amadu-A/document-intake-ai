# tests/test_retention_history.py
"""24 часа ожидания/результатов, sliding cookie и изолированная история без браузера."""

from datetime import timedelta

from app.domain.jobs import utcnow
from app.worker import process
from tests.support import pdf_bytes
from tests.test_boundary_api import FOREIGN, KEY


def set_clock(monkeypatch, value):
    """Согласует часы слоёв для TTL-тестов, не ускоряя настоящие OCR/worker таймауты."""
    for module in (
        "app.domain.jobs",
        "app.infrastructure.database",
        "app.infrastructure.boundary_transactions",
        "app.infrastructure.security",
        "app.application.exports",
        "app.application.intake",
    ):
        monkeypatch.setattr(module + ".utcnow", lambda: value)


async def test_session_survives_confirmation_processing_and_result_retention(
    client, container, monkeypatch
):
    """Возвращение после почти суток ожидания и ещё почти суток хранения сохраняет owner."""
    initial = utcnow()
    await client.get("/")
    token = client.cookies["intake_session"]
    owner = container.capabilities.session_owner(token)
    headers = {"Origin": "http://testserver", "X-CSRF-Token": container.capabilities.csrf(token)}
    uploaded = await client.post(
        "/api/v1/uploads",
        headers=headers,
        files={"files": ("synthetic.pdf", pdf_bytes(), "application/pdf")},
    )
    assert uploaded.status_code == 202
    identifier = uploaded.json()["job_id"]
    await process(container, identifier)
    job = await container.database.get(identifier)
    assert abs((job.expires_at - initial).total_seconds() - 86400) < 5
    assert job.owner == owner
    set_clock(monkeypatch, initial + timedelta(hours=23, minutes=58))
    revision = (await client.get(f"/api/v1/jobs/{identifier}/segmentation")).json()["plan_revision"]
    manual = await client.post(
        f"/api/v1/jobs/{identifier}/segmentation/actions",
        headers=headers,
        json={"expected_revision": revision, "action": "manual"},
    )
    assert manual.status_code == 200
    confirmed = await client.post(
        f"/api/v1/jobs/{identifier}/segmentation/confirm",
        headers=headers,
        json={"expected_revision": manual.json()["plan_revision"]},
    )
    assert confirmed.status_code == 202
    await process(container, identifier)
    job = await container.database.get(identifier)
    assert (job.expires_at - job.completed_at).total_seconds() == 86400
    assert client.cookies["intake_session"] == token
    set_clock(monkeypatch, job.completed_at + timedelta(hours=23, minutes=59))
    available = await client.get(f"/api/v1/jobs/{identifier}/extractions")
    assert available.status_code == 200
    assert "max-age=259200" in available.headers["set-cookie"].lower()
    assert await container.database.session_active(owner)
    assert (await client.get("/api/v1/jobs")).json()["jobs"][0]["job_id"] == identifier
    link = available.json()["documents"][0]["attachment_file"]["file_url"]
    set_clock(monkeypatch, job.expires_at)
    assert (await client.get(f"/api/v1/jobs/{identifier}/result")).status_code == 410
    assert (await client.get(link)).status_code == 410
    assert not (await client.get("/api/v1/jobs")).json()["jobs"]


async def test_legacy_cookie_upgrade_preserves_owner_and_saved_prompts(client, container):
    """Смена формата capability на root GET не теряет старые задания или overrides."""
    value = "synthetic-legacy." + str(int(utcnow().timestamp()) + 86400)
    token = (
        value
        + "."
        + container.capabilities._mac(container.settings.session_secret.get_secret_value(), value)
    )
    owner = container.capabilities.session_owner(token)
    await container.database.ensure_session(owner)
    custom = {
        **container.prompts.base,
        "extraction": container.prompts.base["extraction"] + "\nУточнение пользователя",
    }
    await container.prompts.save(owner, custom)
    client.cookies.set("intake_session", token)
    await client.get("/")
    # Cookie store может хранить старый hostless cookie вместе с новой host-only записью.
    upgraded = next(c.value for c in client.cookies.jar if c.value.startswith("v2."))
    assert container.capabilities.session_owner(upgraded) == owner
    assert (await container.prompts.get(owner))["prompts"] == custom


async def test_history_and_expired_session_cannot_revive_other_owner(
    client, container, monkeypatch
):
    """Пагинация не выдаёт чужой job, а истёкшая signed identity не продлевается повторно."""
    response = await client.post(
        "/api/v1/uploads",
        headers=KEY,
        files={"files": ("synthetic.pdf", pdf_bytes(), "application/pdf")},
    )
    identifier = response.json()["job_id"]
    assert not (await client.get("/api/v1/jobs?offset=0&limit=1", headers=FOREIGN)).json()["jobs"]
    assert (await client.get(f"/api/v1/jobs/{identifier}", headers=FOREIGN)).status_code == 404
    await client.get("/")
    token = client.cookies["intake_session"]
    owner = container.capabilities.session_owner(token)
    set_clock(monkeypatch, utcnow() + timedelta(seconds=container.settings.session_ttl_seconds + 1))
    assert (await client.get("/api/v1/jobs")).status_code == 401
    assert not await container.database.renew_session(owner)
    await client.get("/")
    assert client.cookies["intake_session"] != token
