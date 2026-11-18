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

Архив доступен после `completed`/`completed_with_warnings`; до этого `409 archive_not_ready`.
Подтверждение состава остаётся отдельным `POST .../segmentation/confirm` с сохранённой
ревизией. Web-кнопка «Разрезать и скачать» вызывает его, ожидает готовности через
snapshot/SSE и подготавливает ZIP. Headless-клиент использует те же endpoints.
POST архива не держит запрос на время OCR/VLM и не запускает распознавание повторно.
GET также может собрать отсутствующий кеш. У архивов обычные owner/capability/TTL,
checksum, byte length и запрет доступа после истечения срока, даже до физической очистки.

Порядок ZIP — глобальный `segment_index` пачки, независимо от исходного файла.
Имя PDF: `doc_<segment_index>_<вид>[_<номер>][_<дата ISO>].pdf`.
Для письма вид — «Входящее письмо»/«Исходящее письмо», при неизвестном направлении —
«Письмо». Для неписьма — проверенный `document_kind`, при неудаче классификации —
«Документ». Пробелы/недопустимые знаки путей заменяются на `_`, управляющие символы
удаляются, длинные компоненты ограничиваются для Windows. Номер и дата — собственные
реквизиты документа; связанные договоры/счета не подставляются вместо отсутствующих.
Эти же имена выдаются в `attachment_file.file_name` и отдельных download headers.
Поля JSON 1.0 сохраняются; новые реквизиты неписем этим изменением не извлекаются.

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
| `POST /api/v1/jobs/{id}/archive` | Подготовить ZIP; `file_name`, `byte_size`, `sha256`, подписанный `file_url` |
| `GET /api/v1/jobs/{id}/archive` | Скачать ZIP всех готовых PDF по owner/подписи |
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
Режим входит в hash. После изменения default старый ключ автоматического запроса
повторяется с явным `auto_split=true`; тот же ключ с новым default даёт конфликт,
а не возвращает автоматически обработанное задание вместо review.
`/uploads` не фиксирует промпты в hash: snapshots снимаются при start/confirm.

Статусы: `uploaded`, `queued`, `running`, `awaiting_confirmation`, `completed`, `completed_with_warnings`,
`failed`, `cancelled`. После TTL доступ немедленно запрещён с `410 job_expired`;
после физического удаления записи возможен `404`. Чужой owner получает `404`.
При ошибке распознавания статус содержит безопасный `error` и ссылки на исходники;
`/result` не возвращает сырой или ошибочный JSON как успешный результат.

SSE-события имеют возрастающий `id`. `text_delta` содержит `validated: false`:
это поток модели для просмотра, его нельзя использовать как реквизиты для регистрации.
После reconnect клиент читает snapshot и продолжает с cursor. Итог получает отдельно.

## Подтверждение по умолчанию: Web, 1С и Telegram

1. `POST /api/v1/jobs`, multipart `files` (повторяется для каждого файла), необязательный
   `organization_hint`; сохранить `job_id` из ответа `202`.
2. `GET /api/v1/jobs/{job_id}?include_pages=false` до `awaiting_confirmation`.
   Извлечение ещё не началось; `/result` возвращает `409 result_not_ready`.
3. `GET /api/v1/jobs/{job_id}/segmentation`: показать пользователю `proposed_plan`,
   `draft_plan`, `plan_revision`, `can_confirm`, `comparisons`. Страницы адресуются
   реальными `file_id` и физическими `page_number`.
4. Необязательно: `PUT /api/v1/jobs/{job_id}/segmentation` с последней
   `expected_revision`, исправленными `documents`, `excluded_pages`, `action="edit"`.
   Получить новую `plan_revision`; исходное предложение сохраняется отдельно.
5. `POST /api/v1/jobs/{job_id}/segmentation/confirm`, JSON
   `{"expected_revision": 4}`, где 4 — фактическая последняя ревизия. Ответ `202`;
   повтор той же ревизии идемпотентен, устаревшая ревизия даёт `409`.
6. Poll до `completed`/`completed_with_warnings`, затем
   `GET /api/v1/jobs/{job_id}/result`, скачать реальные PDF по
   `files[].documents[].attachment_file.file_url`. Проверить SHA-256/размер из status.

Это намеренное изменение API: отсутствие flags больше не включает Auto ни для
одной интеграции. `/start` не обходит подтверждение. Для полностью автоматического
сценария передавайте `auto_split=true` явно; неизвестные переходы всё равно требуют
проверки. Существующие задания сохраняют явно сохранённые параметры.

## Необязательные режимы и редактор

Оба флага передаются полями multipart `/jobs` и `/uploads`:

| `auto_split` | `skip_segmentation` | Поведение |
|---|---|---|
| `false` (default) | `false` (default) | VLM предлагает состав; `awaiting_confirmation`, затем confirm |
| `true` | `false` | Явно выбран Auto: VLM определяет состав, затем извлекает факты |
| `false` | `true` | Один файл — одна группа; прямой `/jobs` не требует confirm |
| `true` | `true` | `422 invalid_segmentation_mode` |

| Запрос | Контракт |
|---|---|
| `GET /jobs/{id}/segmentation` | Flags, source, `plan_revision`, proposal/draft/approved, `can_confirm` |
| `PATCH /jobs/{id}/mode` | JSON `{expected_revision, auto_split, skip_segmentation}` |
| `PUT /jobs/{id}/segmentation` | JSON `{expected_revision, documents, excluded_pages, page_order?, action?}` |
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
Auto/review сначала выполняют Docling OCR всех страниц, затем N−1 текстовых
сравнений соседей каждого файла. Изображения при определении границ не передаются.
После подтверждения используются сохранённые OCR и vision-извлечение.
`excluded_pages` присутствует только в plan/status-счётчиках/manifest; JSON 1.0 неизменен.

UI использует `include_pages=false` для status/snapshot/SSE; изображения получает через
пагинацию. Legacy default `include_pages=true` сохраняется. Новые события:
`source_discovered`, `page_ready`, `preview_ready`, `mode_changed`, `draft_saved`,
`plan_approved`, `plan_locked`; пути файловой системы в SSE не выдаются.
Дополнительно: `ocr_retry` (attempt/http_status/retry_after_seconds),
`ocr_progress`, `extraction_progress` — фактические `{completed,total}` без OCR
и ответов модели. Status/snapshot сохраняет их в `work_progress.ocr` и
`work_progress.extraction`; неизвестный счётчик отсутствует. `work_stage` сохраняет
последний выполнявшийся этап, в том числе при ошибке/отмене. Превью отдельно:
`pages_ready`/`pages_total`. Общий legacy `progress` не является процентом OCR.
`pair_started` и `pair_result` (index, адреса соседей, status, attempts, solution,
error_code, duration_seconds; без OCR/отпечатка). Порядок событий пар может отличаться
от физического порядка; использовать index и PageRef.

Status/snapshot и segmentation содержат `comparisons`:
`total`, `completed`, `errors`, `needs_review`, `transitions[]`.
У unresolved перехода `solution=null`, а код объясняет причину. Running/pending
не являются решениями. Успешный ответ имеет строго boolean и status=completed.
После исчерпания ограниченных повторов или бюджета этапа статус —
`awaiting_confirmation`, source — `vlm_partial`, `plan_locked=false`.
Частичный draft соединяет только подтверждённые true; проблемные стыки показаны отдельно.
Флаги исходного auto/review сохраняются. Нельзя считать provisional разрыв ответом false.
До явного confirm `/result` недоступен; после — извлекается ровно утверждённый состав.
В автоматическом режиме без ошибок confirm не нужен. Skip не выполняет сравнения;
ручная правка по-прежнему включает skip. Разные источники автоматически не объединяются.

В ручном составе несмежный список страниц допустим: группа [101,103] не включает лист 102. Порядок
листов внутри PDF по умолчанию исходный. В UI Ctrl/Cmd выбирает отдельные листы,
«Создать диапазон» переносит выбранные страницы из прежних групп в новую.

`page_order` — необязательный полный список PageRef в операторском порядке;
`null`/отсутствие означает исходную последовательность. Если список задан, он содержит
каждую страницу пакета ровно один раз, включая `excluded_pages`. Потери, дубли и
чужие адреса дают `422 invalid_page_order`. `documents[].pages` и сами documents
должны следовать этому порядку; все прежние проверки покрытия/лимитов сохраняются.
У PUT есть действие аудита `action="page_move"`. Только явное сохранение ручного
плана разрешает перестановку; автоматическое разделение не меняет исходную последовательность.
До готовности всех превью новый порядок отклоняется с `409 preview_not_ready`,
после confirm — с `409 plan_locked`; owner, TTL и optimistic revision проверяются как прежде.

GET segmentation возвращает `page_order` в draft/approved/initial планах.
После перестановки интеграция передаёт его при последующих правках всего плана.
При confirm список копируется в immutable snapshot. PDF-комплект, manifest и
`documents[].pages` JSON 1.0 следуют выбранному порядку; номера остаются физическими
и могут убывать. Исходный файл не переписывается.
Каталог GET pages и его `offset/global_position` сохраняют первоначальный порядок;
для нового представления используйте `page_order` и ссылки по PageRef. Это исключает
подмену превью при переносе страницы через границу порций по 60 элементов.

Поворот большого web-превью — настройка просмотра вкладки по PageRef; он не меняет
серверную ревизию/план, OCR, JSON, оригинал или PDF-комплект. В sessionStorage
сохраняются только job ID/PageRef/углы; изображения и OCR туда не записываются.

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

Каждый заблокированный диапазон — отдельный блок: сначала VLM определяет род/вид/подвид,
затем извлекает реквизиты письма по выбранной классификации. PDF всегда содержит весь
этот блок. В запросе не более восьми физических страниц; `block_id`, индекс/число частей,
`has_previous`/`has_next` обозначают продолжение. Части проходят проверку и согласование VLM.

`GET status` и `snapshot` дополнительно возвращают `document_processing` — словарь
по `document_id`. В `artifacts[].processing` находится та же диагностика:

| Поле | Значение |
| --- | --- |
| `segment_index` | Номер регистрационного блока от 1 |
| `status` | `processing`, `recognized`, `unsupported_template`, `classification_failed`, `extraction_failed` |
| `stage` | `classification` либо `extraction` |
| `code` | `null` или диагностический код, например `template_not_supported`, `schema_validation_failed` |
| `parts_completed`, `parts_total` | Проверенные части текущего этапа; при переходе к извлечению счётчик начинается заново |

Поддержан только шаблон писем. `unsupported_template` сохраняет установленный тип,
краткое описание, PDF и предупреждение `[template_not_supported]`; остальные реквизиты
и `one_c` пусты, кроме заданной обслуживаемой организации. Ошибки одного блока после
ограниченных повторов дают `classification_failed`/`extraction_failed`: неподтверждённые
поля пусты, предупреждение и `low_confidence_fields` явно обозначают неполноту.
Успешные блоки сохраняются; пакет завершается `completed_with_warnings`, `/result`
возвращает JSON 1.0 со всеми блоками и соответствующими warnings.
Перед регистрацией интеграция проверяет статус нужного блока: наличие PDF само по
себе не подтверждает распознавание. Отмена, общий timeout и техническая ошибка задания
сохраняют прежние terminal статусы и ограничения выдачи `/result`.

SSE `document_processing` и `document_completed` содержат только `document_id` и
короткий объект `processing`. Внутренние результаты/пути/OCR в эти события не включаются.
Готовые результаты блоков сохраняются в приватном checkpoint; восстановление worker
не повторяет VLM для того же снимка промпта и страниц. Файл скачивается через прежний
подписанный URL с проверкой owner и TTL.
Без контекста обслуживаемой организации направление `null`, `template_path=[]` и warning.
Для писем с известным направлением вид/подвид и конечный `template_path` проверяются
закрытым каталогом. У неписем направление также может быть incoming/outgoing,
если организация подтверждена сторонами документа; юридический род сохраняется,
`template_path=[]`, маршрут и шаблон 1С остаются null.

Для распознанных писем `one_c` — программная проекция проверенных фактов: `organization` из доверенного
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

Пример независимого Python-клиента с явно выбранным Auto (ключ помещён в окружение интеграции;
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
            data={"organization_hint": "ООО «Взлет-МСК»", "auto_split": "true"},
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
