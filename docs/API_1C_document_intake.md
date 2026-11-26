<!-- docs/API_1C_document_intake.md -->
# Document Intake AI — инструкция программисту 1С

Реализованный интерфейс по ТЗ от 25.11.2026 на основе main 54b6424. Три маршрута реестра проверяются вместе с остальным REST API. Они появятся на сервере после штатного обновления владельцем. Локальная страница инструкции: `/integration/1c`, исходный Markdown: `/integration/1c.md`. Контракт опубликован также в `/openapi.json` и `docs/openapi.json`.

Базовый URL: `http://192.168.55.3:8084`. API: `/api/v1`. Swagger: `http://192.168.55.3:8084/docs`; спецификация: `http://192.168.55.3:8084/openapi.json`.

## Заголовки

| Заголовок | Значение | Когда |
|---|---|---|
| `X-API-Key` | Ключ интеграции 1С, выданный администратором | Все owner-запросы |
| `Idempotency-Key` | Уникальный ключ конкретной операции загрузки | Рекомендуется при POST jobs/uploads; повторять тот же ключ при сетевом сбое того же запроса |
| `Content-Type` | `multipart/form-data; boundary=...` | Загрузка файлов |
| `Content-Type` | `application/json` | JSON-тело редактирования/подтверждения/режима/промптов |

Не хранить секреты в исходном коде или логах, не передавать ключ в query. У cookie-клиентов отдельный механизм CSRF; 1С с API-ключом cookie не нужна. При необходимости защищённого транспорта обращаться через согласованный внутренний HTTPS/VPN; не публиковать порт в интернете. Один ключ соответствует namespace интеграции; 1С сама разграничивает доступ своих пользователей к job_id.

## Основной сценарий

1. **Загрузить PDF** — `POST /api/v1/jobs`, multipart. Обязательный `files` (один или несколько повторяющихся частей с этим именем); необязательные `organization_hint` (строка), `auto_split=false`, `skip_segmentation=false`. `Idempotency-Key` — заголовок, не поле тела. Два true запрещены. По умолчанию сначала предложение диапазонов, затем подтверждение. Ответ `202 StatusView`, сохранить `job_id`, `expires_at`, идентификатор пользователя и ключ операции в 1С. Повтор того же содержимого с тем же ключом возвращает то же задание; изменённое содержимое — 409. Не нужно держать HTTP-запрос открытым на время OCR.

   ```bash
   curl -X POST 'http://192.168.55.3:8084/api/v1/jobs' \
     -H 'X-API-Key: KEY' -H 'Idempotency-Key: intake-unique-001' \
     -F 'files=@incoming.pdf' \
     -F 'organization_hint=ООО «Наша организация»' \
     -F 'auto_split=false' -F 'skip_segmentation=false'
   ```

   Альтернатива: `POST /api/v1/uploads` с теми же полями, затем `POST /api/v1/jobs/JOB/start` без тела. При использовании `/uploads` дождаться готовности превью; `/jobs` сам запускает анализ.

2. **Получать состояние** — `GET /api/v1/jobs/JOB?include_pages=false`, `200 StatusView`. Опрос каждые 2–5 секунд для активного окна, фоновый опрос с разумным интервалом и backoff. Сохранять job_id между сеансами. Статусы: uploaded, queued, running, awaiting_confirmation, completed, completed_with_warnings, failed, cancelled. Показывать stage/work_stage, work_progress, comparisons, deadlines и error; `expires_at` может быть null во время работы. `GET /api/v1/jobs/JOB/snapshot?include_pages=false` — полный снимок. Не запускать новое задание из-за длительного queued. При `awaiting_confirmation` уведомить сотрудника в 1С один раз, дальнейшая работа ждёт его действия. Не считать неизвестные границы завершённым извлечением.

3. **Получить диапазоны** — `GET /api/v1/jobs/JOB/segmentation` (200 SegmentationView). Читать `proposed_plan`, `draft_plan`, `plan_revision`, `can_confirm`, `locked`, `comparisons`. Для отображения страниц `GET /api/v1/jobs/JOB/pages?offset=0&limit=60`; метаданные, preview_url и thumbnail_url. Физический адрес страницы `(file_id,page_number)` не зависит от позиции в ленте.

4. **Изменить диапазоны при необходимости** — `PUT /api/v1/jobs/JOB/segmentation` (`application/json`). Пример для исходного PDF из двух страниц, разрезанных на два документа:

   ```json
   {
     "expected_revision": 3,
     "documents": [
       {"client_id":"doc-a","pages":[{"file_id":"SOURCE_ID","page_number":1}]},
       {"client_id":"doc-b","pages":[{"file_id":"SOURCE_ID","page_number":2}]}
     ],
     "excluded_pages": [],
     "page_order": [
       {"file_id":"SOURCE_ID","page_number":1},
       {"file_id":"SOURCE_ID","page_number":2}
     ],
     "action":"edit"
   }
   ```

   `expected_revision` и file_id взять из фактических ответов. Все физические страницы назначены ровно один раз либо исключены; `page_order`, если передан, содержит все страницы, включая исключения. После 200 взять новую `plan_revision`. 409 — перечитать состояние, не переписывать чужую редакцию. Если изменений нет, PUT не нужен. Отдельные команды `POST /api/v1/jobs/JOB/segmentation/actions` принимают `expected_revision`, `action` (split/merge/create/exclude/restore/move/manual/reset), `pages`, `target`, `side` согласно фактическому Swagger. `PATCH /api/v1/jobs/JOB/mode`: `{"expected_revision":3,"auto_split":false,"skip_segmentation":true}`; только при доступной разметке, меняет режим, не подтверждает её.

5. **Утвердить состав** — `POST /api/v1/jobs/JOB/segmentation/confirm` с `{"expected_revision":4}` (фактическая ревизия). Ответ 202 StatusView. Повтор уже утверждённой ревизии безопасен. Только после подтверждения запускается извлечение в default режиме. Сотрудник закрывает форму, 1С хранит job_id. После `completed`/`completed_with_warnings` уведомляет сотрудника; проверяет `document_processing` и warnings каждого блока. `failed/cancelled` — отдельные уведомления, читать `error.code/message`, не считать результат готовым.

6. **Получить реквизиты** — `GET /api/v1/jobs/JOB/result` (200 `BatchResult`, schema_version=1.0) и/или `GET /api/v1/jobs/JOB/extractions` (200 `DetailedResult`, schema_version=2.0). V1: `files[].documents[]`, V2: `documents[]`. Сопоставление по document_id, segment_index, source_pages (v2). `classification.document_kind` — точный вид, `classification.document_root` — верхняя категория; v2 `kind_code/name/path` — маршрут-кандидат Dxx, не GUID. `requisites.document_number/date` — собственные номер и дата; `sender/recipient` — фактические стороны; `one_c` — проекция, не регистрация. `one_c.counterparty` и `organization` могут быть null при отсутствии контекста. Нельзя автоматически подменять их продавцом. Денежные данные v2 — decimal strings. Устаревший v1-only job может вернуть 409 на `/extractions`; не препятствует реестру на основе v1.

7. **Получить реестр**:

   ```http
   GET /api/v1/jobs/JOB/registry HTTP/1.1
   X-API-Key: KEY
   ```

   Ответ 200 `RegistryResult`: `schema_version`, `job_id`, `items[]`. В каждой строке `document_id` для связи с реквизитами и PDF и 14 ключей Excel. Пример:

   ```json
   {
     "schema_version":"1.0",
     "job_id":"JOB",
     "items":[{
       "document_id":"DOC",
       "number":1,
       "date":null,
       "delivery_method":null,
       "receipt_city":null,
       "recipient":"ООО «Покупатель»",
       "document_name":"Счёт-фактура",
       "sender":"ООО «Продавец»",
       "document_number":"0002647329 от 27.11.2025",
       "opening_date":null,
       "scanning_date":null,
       "registration_date":null,
       "responsible":null,
       "notes":null,
       "document_link":null
     }]
   }
   ```

   `GET /api/v1/registry/schema` → 200 `schema_version`, `column_order`, `columns`: JSON-словарь `[заголовок Excel, путь к фактам либо false]`. Единственный источник — `app/domain/registry_mapping.json`. `false` означает не извлекать из OCR, `number` генерируется сервисом, остальные такие поля заполняет 1С/сотрудник. Поля даты в JSON — ISO. `document_number` — форматированное представление, для поиска в 1С использовать исходные номер и дату из requisites (не разбирать отображаемую строку). Получение реестра не запускает обработку и не регистрирует документы.

   `GET /api/v1/jobs/JOB/registry.xlsx` с `X-API-Key` → XLSX, 14 столбцов; MIME `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`. Сохранить бинарное тело, проверить Content-Length и X-Content-SHA256, учитывать X-Expires-At. Книга идентична JSON по строкам и значениям. Отсутствующие поля пустые. При необходимости заполнить их вручную или в 1С. Не путать даты получения и документа; не заменять адресом стороны город получения. Реестр содержит все утверждённые блоки, включая неопределённое направление/ошибки распознавания: 1С сама определяет, какие регистрировать.

   В status.links находятся `registry` и `registry_xlsx`. Реестр доступен в completed и completed_with_warnings, также для старого v1-only результата. Порядок строк — глобальный segment_index; вложения внутри блока не создают отдельные строки. Ненадёжные поля и реквизиты блоков с classification_failed/extraction_failed/unsupported_template пусты. Подтверждённый вид сохраняется, если не помечен ненадёжным. delivery_method допускает только явные значения почта/почтой/mail/post/postal_mail → «Почта», курьер/курьером/courier → «Курьер»; unknown, email и инструкции о будущем отправлении дают null. Это форматирование сохранённого факта, не новая интерпретация PDF.

   Лист «Реестр», A:N, фильтр и закреплённая шапка. Ручные date/receipt_city/opening_date/scanning_date/registration_date/responsible/notes/document_link всегда null: сервис не подставляет время обработки или подписанную ссылку на PDF. number генерируется от 1. Номер и дата документа выводятся только в document_number; без номера остаётся «от ДД.ММ.ГГГГ», без даты — исходный номер, без обоих — null. Ведущие нули и оригинальные слеши сохраняются. XLSX требует owner-ключ либо собственную cookie-сессию; capability-ссылка не предусмотрена. Повторный GET не меняет данные и не запускает AI, но упаковка XLSX может иметь другую контрольную сумму; проверять checksum именно текущего ответа. TTL проверяется также после генерации книги.

8. **Скачать документы** — `GET /api/v1/jobs/JOB/documents/DOC/file`; `GET /api/v1/jobs/JOB/documents/DOC/manifest` — физические страницы и checksum. ZIP: `POST /api/v1/jobs/JOB/archive` без тела → 200 ArchiveView (`file_name`, `byte_size`, `sha256`, `file_url`), затем GET по URL; либо прямой `GET /api/v1/jobs/JOB/archive` с ключом. PDF/ZIP имена соответствуют полному виду, номеру, дате. Подписанный URL действует не дольше пяти минут и TTL; обновлять ссылку через авторизованный запрос. Не записывать capability URL в лог и не использовать его как ссылку в ДО. Сохранить документы в 1С до истечения TTL.

9. **Регистрация в 1С** — внешняя операция, не вызов VLM. 1С проверяет контрагентов/организацию по справочнику, ищет дубли и регистрирует/архивирует по решению пользователя. Фактическая дата получения, город, ответственный, дата регистрации и ссылка ДО заполняются в 1С. Нового endpoint обратной записи этих полей в сервис в данном этапе нет.

## Дополнительные существующие операции

Извлечение нескольких утверждённых блоков выполняется ограниченно параллельно.
В status/snapshot `work_progress.extraction.completed/total` — уникальные завершённые
документы, `extraction_active` — фактически активные документы этого пакета.
Порядок результатов определяется segment_index, порядок завершения не имеет значения.
Повторное подключение/опрос восстанавливает те же счётчики. JSON 1.0/2.0 и реестр
не меняют форму. В Web поле организации скрыто; organization_hint через API сохранён.

| Метод и URL (prefix /api/v1) | Аргументы | Результат |
|---|---|---|
| GET /jobs | offset=0, limit=20 | JobList; восстановление своих задач |
| POST /jobs/JOB/cancel | без тела | StatusView |
| GET /jobs/JOB/events | after=cursor или Last-Event-ID | SSE; необязательно, polling достаточен |
| GET /session/prompts | нет | PromptView |
| PUT /session/prompts | JSON segmentation, extraction — полные строки | PromptView |
| POST /session/prompts/{stage}/restore | stage=segmentation или extraction | PromptView |

Уточнять допустимые лимиты и фактические схемы в Swagger. Промпты задания фиксированы при запуске; изменение текущего owner-профиля не переписывает запущенные jobs. По умолчанию ожидание подтверждения и хранение результата по 24 часа, но читать реальный expires_at. 

## Ошибки

Ожидающее начала задание имеет stage=queued; Web показывает «Ожидает начала обработки». ACK RabbitMQ выполняется после транзакционного claim, до длительной работы. Предел ожидания по умолчанию 14400 с, подготовки 3600 с, анализа 14400 с; это независимые бюджеты, а не сроки хранения файлов. Старые сохранённые deadlines автоматически не продлеваются.

Ошибка writer даёт 503 registry_export_failed, недостаток места — 507 storage_insufficient. Повторить только GET реестра с backoff, не отправлять документ на распознавание заново. При 410 скачать прежний файл уже нельзя; 1С должна сохранять документы до TTL.

`{"error":{"code":"registry_not_ready","message":"Реестр доступен после извлечения реквизитов"}}` — ответ до готового результата. Обрабатывать 401 (ключ), 403 (доступ/подпись), 404 (нет/чужое задание), 409 (not-ready, revision, lock, v2 unavailable), 410 (TTL), 413 (upload limit), 422 (формат), 429 (учесть Retry-After), 503 (временный сбой), 507 (место). Для 408 загрузки проверить состояние по прежнему ключу. Для 5xx повторять только безопасные операции с backoff; для upload — тот же Idempotency-Key и те же байты, для confirm — та же фактическая ревизия. 409 до готовности не повод заново загружать PDF. Ошибки worker `queue_timeout`, `preview_timeout`, `job_timeout` и `worker_attempts_exhausted` приходят в status.error; не бесконечно перезапускать задание. После истечения TTL возвращается 410 или 404 после очистки. Подробная OpenAPI должна быть актуализирована вместе с кодом и протестирована через реальные запросы.
