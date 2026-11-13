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
| `POST /api/v1/uploads` | `202 uploaded`, отдельная подготовка превью без OCR/VLM |
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

`organization_hint` — необязательное полное название обслуживаемой организации,
до 1000 символов, одинаково для `/jobs` и `/uploads`. Оно фиксируется при загрузке,
возвращается в owner-protected status/snapshot и учитывается в идемпотентности.
Web передаёт тот же параметр из необязательного поля перед выбором файлов.
Скрытой организации по умолчанию нет; reload восстанавливает контекст задания.

`Idempotency-Key` — необязательный заголовок submit/upload длиной до 128 символов.
Повтор owner + key + те же файлы/контекст/prompts возвращает прежнее задание.
Другой payload с тем же ключом — `409 idempotency_conflict`. Название файла и порядок
источников входят в hash запроса. Retry повторяет исходные bytes, а не пересозданный PDF.
Для default-режима сохранён прежний hash, поэтому retry ключей до обновления совместим.
`/uploads` не фиксирует промпты в hash: snapshots снимаются при start/confirm.

Статусы: `uploaded`, `queued`, `running`, `awaiting_confirmation`, `completed`, `completed_with_warnings`,
`failed`, `cancelled`. После TTL доступ немедленно запрещён с `410 job_expired`;
после физического удаления записи возможен `404`. Чужой owner получает `404`.
При ошибке распознавания статус содержит безопасный `error` и ссылки на исходники;
`/result` не возвращает сырой или ошибочный JSON как успешный результат.

SSE-события имеют возрастающий `id`. `text_delta` содержит `validated: false`:
это поток модели для просмотра, его нельзя использовать как реквизиты для регистрации.
После reconnect клиент читает snapshot и продолжает с cursor. Итог получает отдельно.

## 3 шага по умолчанию

1. `POST /api/v1/jobs`, multipart `files` (повторяется для каждого файла), необязательный
   `organization_hint`; сохранить `job_id` из ответа `202`.
2. `GET /api/v1/jobs/{job_id}?include_pages=false` до `completed` или
   `completed_with_warnings`; `failed` содержит диагностический код. Долгий анализ
   выполняется worker, HTTP-запрос не удерживается.
3. `GET /api/v1/jobs/{job_id}/result`, затем скачать реальные PDF по
   `files[].documents[].attachment_file.file_url`. Проверить SHA-256/размер из status.

Старые клиенты не передают новые flags и при корректном плане работают автоматически.
Исключение — разрешённый владельцем резервный путь page_fallback, описанный ниже.
Интеграции обязаны различать awaiting_confirmation и успешное завершение.

## Необязательные режимы и редактор

Оба флага передаются полями multipart `/jobs` и `/uploads`:

| `auto_split` | `skip_segmentation` | Поведение |
|---|---|---|
| `true` (default) | `false` (default) | VLM определяет состав, затем извлекает факты |
| `false` | `false` | VLM предлагает состав; `awaiting_confirmation`, затем confirm |
| `false` | `true` | Один файл — одна группа; прямой `/jobs` не требует confirm |
| `true` | `true` | `422 invalid_segmentation_mode` |

| Запрос | Контракт |
|---|---|
| `GET /jobs/{id}/segmentation` | Flags, source, `plan_revision`, proposal/draft/approved, `can_confirm` |
| `PATCH /jobs/{id}/mode` | JSON `{expected_revision, auto_split, skip_segmentation}` |
| `PUT /jobs/{id}/segmentation` | JSON `{expected_revision, documents, excluded_pages, action?}` |
| `POST /jobs/{id}/segmentation/confirm` | JSON `{expected_revision}`, `202` со status |
| `GET /jobs/{id}/pages?offset=0&limit=60` | Порция страниц, limit 1..120, ready/pending и URLs |
| `GET /jobs/{id}/pages/{file_id}/{page}/thumbnail` | Небольшой PNG, тот же owner/signed access |

В таблице общий prefix `/api/v1`. Каждая страница адресуется парой
`(file_id, page_number)`, номер — физический внутри исходника. `global_position`
в page API — отдельный сквозной индекс от 1, `source_order` — порядок источника от 0.
`pending` не имеет фиктивной ссылки; `ready` содержит preview/thumbnail URL.

Пример точного тела сохранения (свои `file_id` брать из status, revision из segmentation):

```json
{
  "expected_revision": 3,
  "documents": [
    {"client_id": "document-a", "pages": [{"file_id": "source-a", "page_number": 1}]},
    {"client_id": "document-b", "pages": [{"file_id": "source-a", "page_number": 3}]}
  ],
  "excluded_pages": [{"file_id": "source-a", "page_number": 2, "reason": "Явно исключено сотрудником"}],
  "action": "split"
}
```

Ответ `200` — актуальный segmentation snapshot: `auto_split=false`,
`skip_segmentation=true`, `segmentation_source=manual` (или `manual_override`),
новая `plan_revision`, `draft_plan`, `proposed_plan`, `approved_plan`, `initial_plan`,
`locked`, `preview_complete`, `can_confirm`, счётчики назначений. Первая ручная правка
сохраняет **и состав, и режим одной транзакцией**. Proposal не изменяется.
`action`: `edit`, `split`, `merge`, `boundary_move`, `exclude`, `restore`, `undo`, `reset`, `manual`.
Undo/Reset отправляют полное восстановленное назначение с текущей revision.

Confirm: `POST .../segmentation/confirm` с `{"expected_revision":4}` возвращает
`202` с queued status. Повтор той же утверждённой revision не ставит второе задание.
Несохранённые/неполные страницы, неизвестные адреса, пересечения, перестановка и
отсутствие регистрационных групп отклоняются. Два конкурентных изменения одной
revision: только одно успешно, второе `409 plan_version_conflict`.
После lock изменение режима/состава даёт `409 plan_locked`.

`/uploads` сразу ставит `work_kind=prepare`: снаружи `uploaded`,
`stage=preparing_preview`, затем `preview_ready`. Первые PNG доступны прогрессивно.
Start во время подготовки фиксирует намерение запустить анализ, подготовка не повторяется.
Review освобождает worker/lease; после confirm используются OCR checkpoints и исходное
предложение. Для ручных групп VLM извлекает внутренние приложения, сохраняя их страницы.
Определение границ работает по изображениям без предварительного OCR всей пачки;
Docling вызывается для извлечения после фиксации состава.
`excluded_pages` присутствует только в plan/status-счётчиках/manifest; JSON 1.0 неизменен.

UI использует `include_pages=false` для status/snapshot/SSE; изображения получает через
пагинацию. Legacy default `include_pages=true` сохраняется. Новые события:
`source_discovered`, `page_ready`, `preview_ready`, `mode_changed`, `draft_saved`,
`plan_approved`, `plan_locked`; пути файловой системы в SSE не выдаются.
Дополнительно: `ocr_retry` (attempt/http_status/retry_after_seconds) и
`segmentation_fallback` (code/revision/page_count).

Если разделение не вернуло корректный план после повторов, auto-план не подтверждён
VLM или истёк `VLM_SEGMENTATION_TIMEOUT_SECONDS`, статус —
`awaiting_confirmation`, flags false/true, source `page_fallback`, `plan_locked=false`.
`draft_plan` содержит по одной группе на физический лист, `fallback.code` в segmentation
и `segmentation_fallback.code` в status объясняют причину. Это технический черновик,
не семантический результат. `/start` и `/result` возвращают 409 до явного confirm;
после правок/подтверждения используется ровно сохранённый состав. Автоматически
подтверждать fallback интеграционным клиентом нельзя.

Несмежный список страниц допустим: группа [101,103] не включает лист 102. Порядок
листов внутри PDF остаётся исходным. В UI Ctrl/Cmd выбирает отдельные листы,
«Создать диапазон» переносит выбранные страницы из прежних групп в новую.

Минутные HTTP-бюджеты независимы: команды RATE_LIMIT_PER_MINUTE=120,
чтение READ_RATE_LIMIT_PER_MINUTE=600, preview/thumbnail PREVIEW_RATE_LIMIT_PER_MINUTE=2400.
Ограничиваются owner и сеть; произвольный query не создаёт новый счётчик.
429 rate_limit содержит Retry-After: 60. Повторять команды без idempotency нельзя.
Docling после ограниченных повторов 429/503 возвращает docling_busy, без upstream payload.
Длительное OCR-окно ожидается через async task с отдельным сроком, без повторных
POST при polling/restart. `docling_task_timeout` и
`docling_task_result_unavailable` отличают истечение ожидания от отсутствующего
результата. Эти статусы не маскируются под успешное распознавание.

Ожидание ручного решения: 3600 секунд после готовности всех превью; правки не продлевают
TTL. Отдельные конечные бюджеты действуют для очереди, превью и анализа. После завершения
начинается новый час хранения. До confirm `/result` отвечает `409`, после TTL — `410`.
Пределы источника, комплекта и пачки перечислены в Settings/`.env.example`; превышение
byte-бюджета — `413 upload_file_limit`/`upload_batch_limit`, страниц — `page_limit`.
Большой body допускается только на multipart upload endpoints; JSON/mode/plan/prompts
имеют отдельный `HTTP_JSON_MAX_MB=4`, включая chunked запросы.

Файлы содержат `Content-Type`, `Content-Length`, `Content-Disposition`, `ETag`,
`X-Content-SHA256`, для завершённых заданий — `X-Expires-At`.
`status.sources` и `status.artifacts` содержат реальные SHA-256/byte_size.
После скачивания файл хранится у клиента независимо от срока хранения нашего сервиса.
Исходник и PDF-комплект — разные артефакты даже при одной регистрационной единице.

Экспорт сохраняет `schema_version`, `batch_id`, `files[].documents[]`, `classification`,
`requisites`, `one_c`, `attachment_file`, `recognition`, `warnings`.
Полного OCR, `one_c_matching`, кандидатов и статусов регистрации 1С нет.
Без контекста обслуживаемой организации направление `null`, `template_path=[]` и warning.
Для писем с известным направлением вид/подвид и конечный `template_path` проверяются
закрытым каталогом. У неписем направление также может быть incoming/outgoing,
если организация подтверждена сторонами документа; юридический род сохраняется,
`template_path=[]`, маршрут и шаблон 1С остаются null.

`one_c` — программная проекция проверенных фактов: `organization` из доверенного
контекста, `counterparty` — текст внешнего отправителя, `comment` — краткое содержание,
`answer_required` — явная необходимость письменного ответа. Явка и удержание доходов
сами по себе не означают ответное письмо. Только письмо получает `view_of_document`
и `template_leaf` из закрытого пути. `folder`, `addressee`, `form`, `received_via`,
`project` без отдельного утверждённого контекста — null. Справочники/GUID 1С не ищутся.
Raw SSE модели содержит ещё не спроектированный `one_c`; интеграция читает `/result`.
Низкая уверенность и неоднозначное прочтение различаются. Явная неоднозначность
требует null, warning и пути в `low_confidence_fields`; противоречие в ответе модели
вызывает ограниченный повтор. Backend не исправляет цифры самостоятельно.
В уже отмеченных моделью сомнительных рукописных номере/дате повтор сохраняет null:
снять неопределённость одним изменением confidence без нового источника нельзя.

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
        if status["status"] == "awaiting_confirmation":
            raise RuntimeError("Требуется проверка состава: " + status["segmentation_source"])
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
