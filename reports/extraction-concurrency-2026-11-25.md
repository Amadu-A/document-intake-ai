<!-- reports/extraction-concurrency-2026-11-25.md -->
# Ограниченное извлечение документов — 25.11.2026

База local/remote main: `2fa677163e58843a11370f44666043f25bf1010f`.
В начале working tree чистый. Актуальные shared guidelines проверены через GitHub;
их содержимое совпало с ранее прочитанными версиями. SSH main/aistation и собственные
healthy API/worker/PostgreSQL подтверждены только чтением. Серверный код, shared
RabbitMQ/Docling/vLLM, prompts и схемы результатов JSON 1.0/2.0 не менялись.
Commit/push/deploy агент не выполнял.

## Что изменено

- Analyze: один фиксированный пул, общий Semaphore документов на composition root,
  независимые DocumentOutcome и детерминированная сборка по segment_index.
- В одном документе классификация, части/согласование, извлечение и уточнения последовательны.
  Число задач ограничено min(число документов, лимит) для каждого активного пакета.
- Checkpoint записывается сразу по завершении документа. Уникальный счётчик и публикация
  сериализованы; checkpoint и накопленный completed сохраняются атомарно в PostgreSQL.
  begin_documents восстанавливает валидные результаты, ID/PDF и удаляет старые статусы.
  Fingerprint, индекс/processing и точная последовательность PageRef сверяются до reuse.
- Общие manifest/groups/files/warnings собираются после завершения задач; имена уникальны
  в порядке пакета. Перестановки/источники/one_c и обычные PDF/ZIP/XLSX сохранены.
- Cancel/deadline/lease останавливают дочерние задачи только своего пакета, ждут cleanup;
  повторная отмена во время дренирования также обработана. Ранний ACK, outbox/fencing сохранены.
- Settings и Compose: DOCUMENT_EXTRACTION_CONCURRENCY=3, VLM_REQUEST_CONCURRENCY=4,
  WORKER_CONCURRENCY=3 (новое явное решение владельца; прежний default был 1).
  PAIR_CONCURRENCY=4 и DOCLING_WINDOW_CONCURRENCY=2 сохранены. Режим 1 использует тот же пул.
- SharedVision ограничивает каждый вызов, включая сравнения и повторы. HTTP pool имеет
  не менее VLM limit + OCR limit соединений (минимум 8). wait_ms локального semaphore
  отделён от http_ms, токены сохраняются только при наличии usage, без текста документов.
  Usage берётся из финального SSE: [контракт vLLM](https://github.com/vllm-project/vllm/blob/main/vllm/entrypoints/openai/chat_completion/serving.py).
- Совместимое поле status/snapshot extraction_active и фактический UI-счётчик. JSON
  документов/реестра не расширен. Блок организации скрыт utility-классом internal-only;
  REST organization_hint и восстановление прежнего контекста сохранены.
- Обновлены OpenAPI и документация. Штатные проверки ставят regular Chromium и headless
  shell; --no-shell убран, поскольку тесты используют оба способа запуска:
  [Playwright](https://playwright.dev/python/docs/browsers#chromium-headless-shell).

Файлы production: app/application/analysis.py, ports.py, exports.py;
app/core/settings.py, container.py; app/domain/jobs.py;
app/infrastructure/database.py, vision.py; app/presentation/http/contracts.py;
templates index/processing, CSS base, JS processing; .env.example, compose.yaml.
Проверки/документация: ops/check-quality.ps1, scripts/test.sh, README, docs/API.md,
architecture.md, operations.md, API_1C_document_intake.md, openapi.json и tests.
Новых production entrypoint-скриптов, зависимостей или миграций нет.

## Реальный сравнительный замер

Один `examples/суд_ув.pdf`: 8 страниц, 5 утверждённых блоков по неизменённому частному
суд_ув_cut.json. Один прогон каждого лимита, последовательно; реальные Docling/shared-VLM.
У каждого запуска новый job/каталог OCR, одинаковый SHA-256 входного PDF и промпты.
Через штатные upload/prepare/edit/confirm/process сформированы все пять PDF и реестр.
Ограничения HTTP/VLM, OCR и модель не перенастраивались между вариантами.

| Лимит документов | OCR, с | Классификация HTTP, сумма с | Извлечение HTTP, сумма с | Уточнения HTTP, сумма с | Пул извлечения wall, с | Всего wall, с | Повторы проверки | Ошибки блоков |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 40,271 | 27,253 | 192,895 | 23,786 | 250,874 | 295,166 | 5 | 0 |
| 2 | 35,292 | 100,961 | 271,933 | 82,951 | 231,903 | 272,426 | 4 | 0 |
| 3 | 40,892 | 165,020 | 341,936 | 81,645 | 225,993 | 270,399 | 3 | 0 |

Наблюдаемый максимум активных документов 1/2/3; все пять checkpoint и восемь страниц
сохранены в каждом прогоне. Время локального VLM semaphore — 0,000 с: лимит 4 здесь
не достигался. Все состояния completed_with_warnings; направление без организации
может оставаться null. Количество технически успешных блоков не доказывает точность
каждого поля частного скана; эталоны не переписывались.

При 3 общее время уменьшилось на **8,4%** (ускорение **1,092×**), этап извлечения —
на **9,9%** (1,110×). При 2 общее время меньше на 7,7%. Это измерение одного пакета,
а не гарантированная кратность для 112 документов. HTTP wall разных запросов
перекрывается; сумму нельзя приравнивать к времени пакета или GPU compute.
Рост времени отдельных VLM-запросов и различие повторов влияют на сравнение;
shared GPU не был изолирован, статистическая серия повторов не выполнялась.

Входные/выходные токены: 235517/19280 (usage у 17 из 18 запросов),
221365/18756 (16 из 17), 220966/18746 (16 из 16). Неизвестные значения не заменены
оценкой. Учтён весь пагинированный журнал, а не первая страница из 100 событий:
первичные агрегаты пересчитаны из сохранённых событий без повторных AI-запросов.
Частные детали: reports/local/extraction-concurrency-live.json, .cache/extraction-live2.log,
.cache/extraction-metrics-complete.log. Эти файлы в Git не включаются.

Первый запуск тестового стенда завершился до извлечения: checkpoint путь Windows
имел 283 символа при существующем каталоге. Для benchmark выбран короткий каталог
внутри pytest basetemp; production Docling и пути сервера не менялись.

## Проверки и ограничения

Новые context-aware HTTP fixtures проверяют production Analyze/SharedVision с настоящими
REST/DB/PDF/ZIP/XLSX. Старые FIFO scripted fixtures явно используют режим 1; проверки
параллельности 3 и defaults независимы от порядка таких заготовок.
Проверены лимиты 1/2/3, три пакета, VLM cap 1/4, завершение не по порядку, совпадение
структуры JSON и содержимого PDF/ZIP/XLSX при одинаковых ответах, уникальные имена,
ошибка одного блока, Cancel/deadline/lease, повторная отмена cleanup, monotonic progress,
invalid fingerprint/index и повторное использование checkpoint без вызова VLM.

Настоящие отдельные RabbitMQ/PostgreSQL: SIGKILL production worker после checkpoint
первого из трёх документов, восстановление с прежними ID и без повторной VLM первого;
отдельно три AMQP worker slots и три пакета, общая сумма активных документов PG <=3,
unacked=0, все девять документов завершены. OCR/VLM этих fault tests синтетические.
Первый fixture не содержал обязательный handwritten_fields и корректно отклонялся;
исправлен ответчик, production validation не ослаблялась.

Windows Python 3.12.15, штатный ops/check-quality.ps1 -Browser: Ruff/diff check прошли,
635 passed, 22 skipped, 638,64 с; покрытие приложения 88%. Отдельные 20 проверок пула,
VLM и конфигурации прошли; отдельные 5 сценариев отмены/deadline/lease/cleanup — также.
Настоящая изолированная доставка: 12 passed за 538,94 с, включая прежние recovery,
publisher-confirm/connection faults, ACK timeout и два новых сценария извлечения.
Linux Python 3.12.16: полный Ruff/pytest --browser — 635 passed, 22 skipped за 815,60 с,
покрытие 88%, без warnings/unhandled exceptions. Собственный локальный Docker-контейнер
без сети, исходники из публичного архива, examples подключены только для чтения; ни .env,
ни секреты в контейнер не передавались. Компоненты штатной Linux-проверки выполнены напрямую:
uv sync --frozen --offline, Ruff check/format и pytest --browser с coverage;
git diff --check выполнен в рабочей копии Windows (в архиве нет .git).
Дополнительно частные Linux-проверки отдельно: 6 passed, включая Chromium/УПД/ZIP.
В каждом полном прогоне 20 opt-in live пропущены штатно, ещё два пропуска относятся
к противоположной ОС. Доставка и новый live-замер 1/2/3 выполнены отдельно, как описано выше;
остальные opt-in live не заявляются выполненными. Compose config --quiet и итоговый
git diff --check прошли. Логи находятся в .cache/extraction-windows-verified.log,
.cache/extraction-linux-all-verified.log, .cache/extraction-delivery-final.log,
.cache/extraction-linux-private-verified.log; в Git не включаются.

Первый полный Windows-прогон выявил ошибку тестового вмешательства во время: прямая
запись deadline в старый row.data могла затереть cursor конкурентного checkpoint.
Тест теперь берёт штатную блокировку repository, проверяет итоговый статус и дренирует
driver в finally. Production транзакции/валидация не ослаблялись. Первые Linux-прогоны
выявили слишком короткие тестовые 5 с до первого VLM: перед ним штатно последовательно
собираются пять PDF в subprocess. Для подготовки в тесте выделено 30 с; после отзыва
fence остановка всех дочерних задач по-прежнему проверяется в пределах 5 с. Рабочие
timeouts/lease сервиса не увеличены.

Снимки tmp/extraction-parallel-active.png и tmp/extraction-parallel-completed.png проверены
визуально на настоящем REST/worker потоке с synthetic VLM;
tmp/extraction-parallel-counter.png — отдельная DOM-проекция
15/112, без выдачи за реальную нагрузку 112 документов. Организация скрыта,
активные документы/отмена/скачивание присутствуют. Не выполнялась ручная работа
в настоящем клиенте 1С/Excel или production-нагрузка из 112 документов.
Реальный перенос/поворот не менялся.

## Команды владельцу

```powershell
cd C:\Users\i.mein\PycharmProjects\document-intake-ai
.\ops\check-quality.ps1 -Browser
git diff --check
git diff --stat
git add -- .env.example README.md compose.yaml app docs ops/check-quality.ps1 scripts/test.sh tests reports/extraction-concurrency-2026-11-25.md
git diff --cached --stat
git commit -m "Bound document extraction concurrency and preserve durable checkpoints"
git push origin main
```

Перед обновлением дождаться активных задач либо согласованно отменить их. Проверить
только свои overrides лимитов: .env.example — defaults, .env/процесс имеют приоритет.
Секреты не менять. После этого:

```bash
cd ~/projects/document-intake-ai
git status --short
git pull --ff-only
bash scripts/test.sh --browser
bash scripts/up.sh
curl --fail --show-error http://127.0.0.1:8084/health/ready
docker compose --env-file .env.example --env-file .env ps
bash scripts/test.sh --live -s tests/test_delivery_live.py tests/test_ack_timeout_live.py tests/test_extraction_delivery_live.py
DOCLING_BASE_URL=http://127.0.0.1:5001 bash scripts/test.sh --live -s tests/test_live_extraction_concurrency.py
```

Для browser нужны системные библиотеки Playwright, для изолированных delivery тестов —
локальные Docker images из docs/operations.md. Все эти команды выводят логи на экран.
Руками: открыть UI с LAN, убедиться в отсутствии блока организации, загрузить пачку,
утвердить состав. При нескольких блоках увидеть «Извлекаем реквизиты», обработанное
количество и активные документы (не больше 3 суммарно процесса). Reload/«Мои задания»
восстанавливают состояние, отмена прекращает активность. После завершения проверить
JSON 1/2, порядок/страницы PDF и ZIP, 14 колонок XLSX, дубли имён с (2). API
organization_hint остаётся доступен. Для режима 1 повторить штатный up.sh с этим
несекретным override, затем вернуть 3; готовые результаты не изменяются.
