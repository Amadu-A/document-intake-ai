# tests/test_upload_limits.py
"""Потоковый HTTP проверяет реальные byte-пределы без большого bytes в памяти клиента."""

import errno

import pytest

from app.domain.errors import IntakeError
from tests.support import pdf_bytes

KEY = {"X-API-Key": "e" * 48}
BOUNDARY = "synthetic-upload-boundary"


async def multipart_sizes(sizes):
    """Передаёт валидный PDF с хвостовым whitespace блоками не более 1 МиБ."""
    prefix = pdf_bytes()
    block = b" " * (1024 * 1024)
    for index, size in enumerate(sizes):
        yield (
            f'--{BOUNDARY}\r\nContent-Disposition: form-data; name="files"; '
            f'filename="test-{index}.pdf"\r\nContent-Type: application/pdf\r\n\r\n'
        ).encode()
        yield prefix
        remaining = size - len(prefix)
        while remaining:
            count = min(remaining, len(block))
            yield block[:count]
            remaining -= count
        yield b"\r\n"
    yield f"--{BOUNDARY}--\r\n".encode()


@pytest.mark.parametrize("extra", [0, 1])
async def test_exact_four_hundred_mib_and_next_byte_through_http(client, container, extra):
    """Реальный parser/ingest принимает 400 МиБ, лишний байт получает 413 ещё до admission."""
    size = 400 * 1024 * 1024 + extra
    response = await client.post(
        "/api/v1/uploads",
        headers={**KEY, "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        content=multipart_sizes([size]),
    )
    if extra:
        assert (
            response.status_code == 413 and response.json()["error"]["code"] == "upload_file_limit"
        )
        assert not list(container.storage.root.iterdir())
    else:
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert response.json()["sources"][0]["byte_size"] == size
        job = await container.database.get(job_id)
        await container.database.cancel(job.id, job.owner)
        await container.storage.remove(job.id)
        assert not list(container.storage.root.iterdir())


async def test_batch_bytes_fail_independently_and_close_all_spools(client, container):
    """Три допустимых файла вместе превышают 1024 МиБ; частичные spools удаляются."""
    mib = 1024 * 1024
    response = await client.post(
        "/api/v1/uploads",
        headers={**KEY, "Content-Type": f"multipart/form-data; boundary={BOUNDARY}"},
        content=multipart_sizes([400 * mib, 400 * mib, 224 * mib + 1]),
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "upload_batch_limit"
    assert not list(container.storage.root.iterdir())


async def test_low_disk_fails_before_temporary_write(client, container, monkeypatch):
    """Недостаток места возвращает диагностический 507 и не оставляет исходник/spool."""

    def low_disk(additional=0):
        """Моделирует исчерпанный резерв, не заполняя настоящий диск."""
        raise IntakeError("storage_insufficient", "Недостаточно места", 507)

    monkeypatch.setattr(container.storage, "_disk_space", low_disk)
    response = await client.post(
        "/api/v1/uploads",
        headers=KEY,
        files={"files": ("test.pdf", pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 507
    assert not list(container.storage.root.iterdir())


async def test_real_disk_full_oserror_becomes_safe_error(client, container, monkeypatch):
    """ENOSPC после предварительной проверки не раскрывает filesystem paths клиенту."""

    def full_disk():
        """Моделирует гонку с заполнением диска при создании временного файла."""
        raise OSError(errno.ENOSPC, "synthetic-private-path")

    monkeypatch.setattr(container.storage, "temporary_upload", full_disk)
    response = await client.post(
        "/api/v1/uploads",
        headers=KEY,
        files={"files": ("test.pdf", pdf_bytes(), "application/pdf")},
    )
    assert response.status_code == 507
    assert "synthetic-private-path" not in response.text


@pytest.mark.parametrize("content_type", ["application/json", "multipart/form-data; boundary=fake"])
async def test_plan_body_has_small_limit_independent_of_file_upload(
    client, container, content_type
):
    """Лимит загрузки 1 ГиБ не разрешает гигантский JSON/ложный multipart в mode API."""
    response = await client.patch(
        "/api/v1/jobs/unknown/mode",
        headers={
            **KEY,
            "Content-Type": content_type,
            "Content-Length": str(container.settings.http_json_max_mb * 1024 * 1024 + 1),
        },
        content=b"{}",
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "upload_limit"


async def test_chunked_json_is_rejected_before_unbounded_body_parsing(client, container):
    """Отсутствие Content-Length не обходит отдельный счётчик JSON body."""

    async def stream():
        """Передаёт немного больше разрешённого JSON без выделения большого payload."""
        block = b" " * (1024 * 1024)
        for _ in range(container.settings.http_json_max_mb + 1):
            yield block

    response = await client.patch(
        "/api/v1/jobs/unknown/mode",
        headers={**KEY, "Content-Type": "application/json"},
        content=stream(),
    )
    assert response.status_code == 413 and response.json()["error"]["code"] == "upload_limit"
