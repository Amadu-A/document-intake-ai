<!-- docs/integration-workflow.md -->
# Полный сценарий 1С, Telegram и других клиентов

Основа — [OpenAPI](openapi.json), интерактивная проверка — `/docs` → Authorize →
свой `X-API-Key`. Swagger не сохраняет ключ между сеансами. Схемы параметров и ответов
генерируются из тех же моделей, что обслуживают запросы. Запросы ниже — учебные;
идентификаторы и `expected_revision` нужно брать из фактических ответов.
`KEY` означает ключ отдельной интеграции, не web-cookie и не owner из тела запроса.
HTTP-примеры не содержат настоящих секретов.

## Загрузка и ожидание

```http
POST /api/v1/jobs HTTP/1.1
X-API-Key: KEY
Idempotency-Key: incoming-2026-0001
Content-Type: multipart/form-data; boundary=packet

--packet
Content-Disposition: form-data; name="files"; filename="incoming.pdf"
Content-Type: application/pdf

<байты PDF>
--packet
Content-Disposition: form-data; name="organization_hint"

ООО «Наша организация»
--packet
Content-Disposition: form-data; name="auto_split"

false
--packet--
```

Ответ `202 StatusView`: сохранить `job_id`. Дополнительные файлы — повторяющиеся
части `files`; `organization_hint` необязателен, отсутствующая организация не угадывается.
Два режима default=false: `auto_split` — явный Auto; `skip_segmentation` — один файл
одним блоком. Оба true запрещены. Для подготовки без анализа используйте те же поля
в `POST /api/v1/uploads`, затем `POST /api/v1/jobs/JOB/start` без тела.

Повтор owner+Idempotency-Key+тот же пакет возвращает прежнее задание; иной пакет
с тем же ключом даёт 409. Отсоединение клиента не отменяет worker. Poll каждые 2–5 с:

```http
GET /api/v1/jobs/JOB?include_pages=false HTTP/1.1
X-API-Key: KEY
```

Ответ `200 StatusView`: `status`, `stage`, реальные `work_progress`,
`comparisons.total/completed/errors`, `plan_revision`, `expires_at`, ссылки.
До `awaiting_confirmation` показывайте прогресс; `failed/cancelled` требуют чтения
`error` и сохранения ссылок на исходники. Полный повторный snapshot —
`GET /api/v1/jobs/JOB/snapshot?include_pages=false`. Не вычисляйте OCR по общему `progress`.

## Просмотр и изменение состава

```http
GET /api/v1/jobs/JOB/segmentation HTTP/1.1
X-API-Key: KEY
```

Ответ `200 SegmentationView`: неизменное `proposed_plan`, редактируемый `draft_plan`,
`initial_plan`, `approved_plan`, `can_confirm`, `locked`, `plan_revision`.
Физические адреса `(file_id,page_number)` не меняются при перестановке.
Превью: `GET /api/v1/jobs/JOB/pages?offset=0&limit=60` → `200 PageSlice`;
PNG по `preview_url`/`thumbnail_url`, источник — по `status.sources[].file_url`.

Серверные операции доступны без JavaScript:

```http
POST /api/v1/jobs/JOB/segmentation/actions HTTP/1.1
X-API-Key: KEY
Content-Type: application/json

{"expected_revision":3,"action":"move","pages":[{"file_id":"SOURCE","page_number":31}],"target":{"file_id":"SOURCE","page_number":32},"side":"after"}
```

Ответ `200 SegmentationView` содержит новую revision. Доступные команды:

| action | Дополнительные поля | Результат |
| --- | --- | --- |
| split | pages с одной страницей либо target | Разрезать её группу после указанного листа |
| merge | pages с листом первой группы либо target | Объединить со следующей группой |
| create | pages | Создать группу только из выбранных страниц |
| exclude | pages | Исключить выбранные листы с сохранением provenance |
| restore | pages | Вернуть выбранные исключённые листы отдельной группой |
| move | pages, target, side=before/after | Перенести в группу целевого листа и изменить порядок |
| manual | нет | Зафиксировать текущий состав вручную |
| reset | нет | Восстановить предложение VLM либо начальные группы по файлам |

Для полной замены или undo используйте `PUT /api/v1/jobs/JOB/segmentation`:

```json
{
  "expected_revision": 4,
  "documents": [
    {"client_id":"a","pages":[{"file_id":"SOURCE","page_number":2},{"file_id":"SOURCE","page_number":1}]}
  ],
  "excluded_pages":[{"file_id":"SOURCE","page_number":3,"reason":"Пустой лист"}],
  "page_order":[{"file_id":"SOURCE","page_number":2},{"file_id":"SOURCE","page_number":1},{"file_id":"SOURCE","page_number":3}],
  "action":"edit"
}
```

Пример относится к исходнику ровно из трёх страниц. Полный `page_order` должен содержать
все физические страницы один раз, включая исключения; состав групп может быть несмежным
и межфайловым. Ответ `200 SegmentationView`. Редактирование атомарно включает ручной режим;
план и режим отдельно на фронтенде не вычисляются. `PATCH /api/v1/jobs/JOB/mode`
с `{"expected_revision":5,"auto_split":false,"skip_segmentation":true}` меняет режим
и также возвращает `SegmentationView`. Устаревшая revision или locked-план — 409.

## Утверждение и результат

```http
POST /api/v1/jobs/JOB/segmentation/confirm HTTP/1.1
X-API-Key: KEY
Content-Type: application/json

{"expected_revision":6}
```

Ответ `202 StatusView`. Повтор утверждённой revision идемпотентен. Poll до
`completed/completed_with_warnings`; проверяйте `document_processing` каждого блока.
Пока результата нет — 409, это не повод повторять загрузку.

```http
GET /api/v1/jobs/JOB/result HTTP/1.1
X-API-Key: KEY

GET /api/v1/jobs/JOB/extractions HTTP/1.1
X-API-Key: KEY
```

Первый ответ — неизменённый `BatchResult` schema_version=1.0; второй —
`DetailedResult` schema_version=2.0. [Полные примеры](samples/result-v2.json).
V2 содержит все физические `source_pages`, фактическую классификацию, серверные
kind_code/name/path, общие реквизиты, типизированные `details`, карту `field_evidence`,
one_c, recognition, warnings и PDF. Денежные/табличные числа v2 — десятичные строки.
Отсутствующее значение — null, неоднозначность — warning, искусственного confidence нет.
Пример `details.profile=D03` для счёта имеет `primary_document_role=payment_request`:
принадлежность папке закрывающих документов не означает подтверждения исполнения.
Неизвестный вид имеет kind_code=null/details=null с общими фактами.
Для старого terminal v1-only задания `/extractions` даёт 409 `extractions_unavailable`:
повторите загрузку для нового распознавания, не считайте старые данные v2.

PDF — `GET /api/v1/jobs/JOB/documents/DOCUMENT/file`; происхождение —
`GET /api/v1/jobs/JOB/documents/DOCUMENT/manifest` → `DocumentManifest`.
Для архива: `POST /api/v1/jobs/JOB/archive` без тела → `200 ArchiveView`, затем
GET по `file_url`, либо сразу `GET /api/v1/jobs/JOB/archive`. Ответ `application/zip`.
ZIP содержит `doc_N/<Полное название> № <номер> от <ДД.ММ.ГГГГ>.pdf`, время создания UTC.
Отдельный PDF имеет то же имя; отсутствующие части опущены, дубликаты имеют ` (2)` и далее.
Проверяйте Content-Length/X-Content-SHA256; заголовки и JSON содержат реальные значения.

Подпись URL действует до пяти минут и не дольше TTL задания. За новой ссылкой можно
обратиться с собственным ключом, пока задание хранится. Файл, скачанный клиентом,
не зависит от TTL сервиса. Токены URL не записывайте в журналы интеграции.

## Восстановление, отмена, события, промпты

Готовый реестр не запускает повторное распознавание:

```http
GET /api/v1/jobs/JOB/registry HTTP/1.1
X-API-Key: KEY

GET /api/v1/jobs/JOB/registry.xlsx HTTP/1.1
X-API-Key: KEY

GET /api/v1/registry/schema HTTP/1.1
X-API-Key: KEY
```

Первый ответ — отдельный RegistryResult schema_version=1.0 с job_id/items,
второй — `Реестр документов.xlsx`, третий — точный каталог 14 колонок и путей фактов.
Строки в порядке segment_index; приложения внутри блока не создают лишних строк.
Ошибочные/неподдержанные блоки остаются в реестре, недостоверные реквизиты пусты.
Все ручные колонки null, кроме сквозного номера; дата получения не равна дате документа.
Собственный номер/дата объединены в отображение `00042/А от 27.11.2025`.
Технический document_id присутствует только в JSON. V1-only исторический результат
поддерживается; до готовности — 409 registry_not_ready. XLSX требует owner-ключ/сессию,
подписанных URL для него нет. Проверять длину, SHA-256 и X-Expires-At из заголовков.
Повтор GET безопасен и не меняет факты; байты ZIP-упаковки XLSX могут отличаться.
Подробная [инструкция 1С](API_1C_document_intake.md), локальная страница `/integration/1c`.

| Запрос (с X-API-Key) | Тело/параметры | Ответ |
| --- | --- | --- |
| GET /api/v1/jobs | offset=0, limit=20 (1..99) | JobList: jobs[], has_more; только своё до TTL |
| POST /api/v1/jobs/JOB/cancel | Нет | StatusView; повтор безопасен, worker прекращает публикацию |
| GET /api/v1/jobs/JOB/events | after=cursor либо Last-Event-ID | text/event-stream; необязательный replay |
| GET /api/v1/session/prompts | Нет | PromptView: prompts, base_prompts, versions |
| PUT /api/v1/session/prompts | {segmentation: полный текст, extraction: полный текст} | PromptView текущего owner |
| POST /api/v1/session/prompts/extraction/restore | Нет | PromptView с восстановленной активной базой |
| POST /api/v1/session/prompts/segmentation/restore | Нет | PromptView с восстановленной активной базой |

Промпты задания — immutable snapshots; изменение настроек не меняет уже запущенное.
SSE `text_delta` всегда неподтверждённый. Источник итоговых реквизитов — только GET result/
extractions. Polling остаётся основным способом интеграции; webhook/WebSocket не нужны.
В браузере «Мои задания» восстанавливает эту же owner-историю без sessionStorage;
другая cookie, браузер или API-ключ имеют отдельную историю.

После завершения/ошибки/отмены данные доступны 24 часа; ожидание утверждения до 24 часов.
Активность продлевает сессию, не TTL задания. После TTL — 410, после физической очистки
возможен 404. Чужой owner всегда получает 404, а не сведения о существовании задания.

Общий ответ ошибки: `{"error":{"code":"plan_version_conflict","message":"Безопасное описание"}}`.
401 — нет ключа/сессии; 403 — CSRF/Origin/подпись; 404 — объект/owner;
409 — revision/lock/not-ready/v2-unavailable; 410 — TTL; 413 — upload limit;
422 — неверное тело/покрытие страниц; 429 — Retry-After; 503 — зависимость;
507 — место на диске. Не повторяйте безусловно 4xx: сначала обновите snapshot.
Cookie-запросы изменений дополнительно требуют Origin и X-CSRF-Token; API-ключу они не нужны.
