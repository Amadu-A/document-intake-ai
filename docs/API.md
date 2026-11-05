<!-- docs/API.md -->
# REST API для 1С и других клиентов

Базовый адрес: `APP_PUBLIC_BASE_URL`, по умолчанию `http://192.168.55.3:8084`.
Swagger и его JS/CSS обслуживаются локально на `/docs`, контракт — `/openapi.json`.
Машиночитаемый экспорт: `docs/result-v1.schema.json`, модели — `app/domain/schema.py`.

Интеграции передают **свой** `X-API-Key`: отдельные записи JSON `API_KEYS`
для `onec`, `telegram`, `evaluation`. Owner не задаётся клиентом и не совпадает между ключами.
Для получения JSON, статуса и manifest нужен ключ того же owner.
Подписанная ссылка даёт доступ только к конкретному файлу до указанного TTL;
для скачивания по ней cookie/дополнительный заголовок не требуются.
Неподписанный download допускается с правильным API-ключом owner.

| Запрос | Результат |
|---|---|
| `POST /api/v1/jobs` | Multipart `files` 1..N, необязательный `organization_hint`; `202` и `job_id` |
| `POST /api/v1/uploads` | Сохранение до запуска, `202`; TTL от загрузки |
| `POST /api/v1/jobs/{id}/start` | Запуск сохранённой пачки, snapshots обоих промптов |
| `GET /api/v1/jobs/{id}` | Статус, этап, прогресс, cursor, timestamps, source/artifact metadata |
| `GET /api/v1/jobs/{id}/snapshot` | Тот же атомарный снимок для reconnect |
| `GET /api/v1/jobs/{id}/events` | SSE; `Last-Event-ID` либо `?after=` |
| `GET /api/v1/jobs/{id}/result` | Только подтверждённый JSON 1.0; до успеха `409` |
| `POST /api/v1/jobs/{id}/cancel` | Идемпотентная отмена; снимает право worker публиковать результат |
| `GET /api/v1/jobs/{id}/source-files/{file_id}` | Неизменённый оригинал |
| `GET /api/v1/jobs/{id}/documents/{document_id}/file` | Целый PDF-комплект регистрации |
| `GET /api/v1/jobs/{id}/documents/{document_id}/manifest` | Полное происхождение страниц и группа VLM |
| `GET /api/v1/jobs/{id}/pages/{file_id}/{page}/preview` | Безопасный PNG-render |
| `GET/PUT /api/v1/session/prompts` | Два текста, базовые версии/тексты текущего owner |
| `POST /api/v1/session/prompts/{stage}/restore` | Восстановить `segmentation` либо `extraction` |
| `GET /health/live`, `GET /health/ready` | Жизнь процесса и реальные проверки зависимостей |

`Idempotency-Key` — необязательный заголовок submit/upload длиной до 128 символов.
Повтор owner + key + те же файлы/контекст/prompts возвращает прежнее задание.
Другой payload с тем же ключом — `409 idempotency_conflict`. Название файла и порядок
источников входят в hash запроса. Retry повторяет исходные bytes, а не пересозданный PDF.

Статусы: `uploaded`, `queued`, `running`, `completed`, `completed_with_warnings`,
`failed`, `cancelled`. После TTL доступ немедленно запрещён с `410 job_expired`;
после физического удаления записи возможен `404`. Чужой owner получает `404`.
При ошибке распознавания статус содержит безопасный `error` и ссылки на исходники;
`/result` не возвращает сырой или ошибочный JSON как успешный результат.

SSE-события имеют возрастающий `id`. `text_delta` содержит `validated: false`:
это поток модели для просмотра, его нельзя использовать как реквизиты для регистрации.
После reconnect клиент читает snapshot и продолжает с cursor. Итог получает отдельно.

Файлы содержат `Content-Type`, `Content-Length`, `Content-Disposition`, `ETag`,
`X-Content-SHA256`, для завершённых заданий — `X-Expires-At`.
`status.sources` и `status.artifacts` содержат реальные SHA-256/byte_size.
После скачивания файл хранится у клиента независимо от срока хранения нашего сервиса.
Исходник и PDF-комплект — разные артефакты даже при одной регистрационной единице.

Экспорт сохраняет `schema_version`, `batch_id`, `files[].documents[]`, `classification`,
`requisites`, `one_c`, `attachment_file`, `recognition`, `warnings`.
Полного OCR, `one_c_matching`, кандидатов и статусов регистрации 1С нет.
Без контекста обслуживаемой организации направление `null`, `template_path=[]` и warning.
Для известного направления вид/подвид и конечный `template_path` проверяются каталогом.

Приложение, только упомянутое в письме, имеет `page_range=[]` и не создаёт отдельный PDF.
Сложная provenance сквозного комплекта находится в manifest; `pages` публичного JSON
содержит реальные физические номера первого source, а не выдуманную общую нумерацию.

Пример независимого Python-клиента (ключ заранее помещён в окружение интеграции;
не передавайте ключ аргументом командной строки и не печатайте ответы с документами):

```python
import hashlib
import os
import time
from pathlib import Path

import httpx

base = "http://192.168.55.3:8084"
headers = {"X-API-Key": os.environ["INTAKE_ONEC_KEY"]}
source_path = Path("incoming.pdf")
with httpx.Client(timeout=120, trust_env=False) as client:
    with source_path.open("rb") as stream:
        response = client.post(
            base + "/api/v1/jobs",
            headers={**headers, "Idempotency-Key": "registration-upload-2026-0001"},
            files={"files": (source_path.name, stream, "application/pdf")},
            data={"organization_hint": "ООО «Взлет-МСК»"},
        )
    response.raise_for_status()
    job_id = response.json()["job_id"]
    deadline = time.monotonic() + 1900
    while time.monotonic() < deadline:
        response = client.get(f"{base}/api/v1/jobs/{job_id}", headers=headers)
        response.raise_for_status()
        status = response.json()
        if status["status"] in {"failed", "cancelled"}:
            raise RuntimeError(status.get("error", {}).get("code", status["status"]))
        if status["status"] in {"completed", "completed_with_warnings"}:
            break
        time.sleep(2)
    else:
        raise TimeoutError("Анализ не завершён")
    response = client.get(f"{base}/api/v1/jobs/{job_id}/result", headers=headers)
    response.raise_for_status()
    result = response.json()
    for artifact in status["artifacts"]:
        download = client.get(artifact["file_url"])
        download.raise_for_status()
        assert hashlib.sha256(download.content).hexdigest() == artifact["sha256"]
        assert len(download.content) == artifact["byte_size"]
        Path(artifact["file_name"]).write_bytes(download.content)
```

Web использует ту же REST API. Cookie mutation дополнительно требует Origin,
равный `APP_PUBLIC_BASE_URL`, и `X-CSRF-Token` из meta главной страницы.
Ключевые ошибки: `upload_limit`, `invalid_mime`, `unsupported_file`, `password_protected`,
`corrupt_document`, `page_limit`, `file_conversion_failed`, `docling_unavailable`,
`docling_incomplete`, `vlm_unavailable`, `invalid_segmentation`, `model_invalid_json`,
`schema_validation_failed`, `queue_full`, `job_timeout`, `access_denied`.
Envelope: `{"error":{"code":"...","message":"безопасное описание"}}`.
