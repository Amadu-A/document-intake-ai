<!-- reports/ocr-window-concurrency-2026-11-18.md -->
# Два параллельных OCR-окна

## Изменение

Один `DoclingOCR` теперь использует общий `asyncio.Semaphore(2)` и ограниченное
число coroutine для окон по 20 страниц. Лимит настраивается через Pydantic
`DOCLING_WINDOW_CONCURRENCY=2`, допускается `1`. Он действует между всеми вызовами
адаптера данного процесса, а не умножается на число обрабатываемых документов.
`WORKER_CONCURRENCY` основных заданий остаётся 1; разные процессы имеют свои лимиты.

`_request_lock` защищает только частоту отправки POST и общий cooldown Retry-After.
Ожидание выполнения/polling и долгий sync POST проходят без удержания этого lock.
Техническое byte-деление окна остаётся в том же слоте; дополнительные дочерние
запросы не умножают параллелизм. После завершения окна обновляется монотонный
прогресс, результаты возвращаются по исходному порядку `(file_id, page_number)`.
Постраничного upstream-прогресса внутри одного OCR-окна по-прежнему нет.

Single-flight lock одного окна действует до записи кеша. При частичном кеше
исходный состав pending окна не меняется: сохраняется его fingerprint/task ID,
restart продолжает ту же задачу. Начатая атомарная запись кеша или task ID
заканчивается до возврата отмены, включая повторную отмену watchdog/gather.
Ошибка/отмена дожидается всех sibling coroutine; фоновых polling не остаётся.
HTTP retry не создаёт новый POST для уже известного принятого task ID.
Результаты Docling по-прежнему проверяются на полное покрытие страниц окна.

В безопасных логах добавлен `ocr_task_timing` с хешем ID, исходом и наблюдаемыми
интервалами очереди/выполнения. Источник — `client_poll`: в публичном status API
Docling Serve 1.38.2 нет времён created/started/finished. Интервалы приблизительны
с точностью polling и сетевых задержек. При ненаблюдавшемся started значения null;
после restart очередь неизвестна, execution отражает только текущее ожидание.
`wait_ms` включает получение результата. Содержимое документов/ключи/полный ID
не логируются. JSON 1.0, REST-контракты, схема БД и VLM-промпты не менялись.

## Фактический runtime

Начальная проверка: локальные main/remote main — `655af6a0`, рабочее дерево чистое.
Прочитаны актуальные shared LLM Context, Engineering Guidelines, Infrastructure
Instructions, services.yaml и compose Docling; действующие инструкции проекта сохранены.

Через `ssh -o BatchMode=yes aistation` подтверждены Ubuntu-пользователь `main`,
существующий здоровый `docling-eval-docling-1`, образ Docling Serve CPU v1.38.2,
сеть `ai-shared`, **2 local worker, 12 CPU, 12 ГиБ**, OMP/MKL по 4 потока.
В актуальном shared `docling-eval/compose.yaml` тоже 12 CPU/12g и 2 worker
(SHA `43a6cc0c`). В `docs/services.yaml` ещё указаны прежние 4 CPU/8g
(SHA `3d2c57ef`); это расхождение реестра с compose/runtime, не причина менять стек.
Shared-конфигурация, исходники сервера, credentials и lifecycle контейнеров не менялись.

## Проверки и замеры

Новые offline-регрессии проверяют настоящие синтетические PDF:

- 173 страницы, 9 окон, умышленное завершение второго окна раньше первого;
  async и sync, максимум два активных запроса и точная привязка OCR каждой страницы;
- два независимых вызова адаптера при лимитах 1/2 и два источника в одной пачке;
- одинаковое окно отправляется один раз; готовый кеш не вызывает повторный HTTP;
- отмена двух активных polling, восстановление обоих task ID без второго POST;
- отмена во время записи принятого task ID и между записями OCR-страниц;
- повторная отмена, отмена соседнего ожидания при отказе одного окна;
- общий cooldown/POST interval и честные client-poll метрики, в том числе null/resumed;
- приоритет `.env.example` → `.env` → process environment и ограничения Settings/Compose.

Полный Windows-прогон штатным `ops/check-quality.ps1`: **436 passed, 16 skipped**,
покрытие **85%**. Полный Linux-прогон в изолированном контейнере без сети:
**434 passed, 18 skipped**, покрытие **85%**. Пропущены явно opt-in live/browser,
неприменимые OS-проверки; в публичном Linux-архиве также нет двух приватных примеров.
После добавления сравнения текстовых/табличных фактов — **23 passed** для целевых
OCR/checkpoint-регрессий. Ruff check/format, `git diff --check` и собственный
`docker compose --env-file .env.example --env-file .env config --quiet` пройдены.

Отдельный реальный тест `test_live_docling_concurrency.py`: **1 passed**,
весь запуск с двумя прогонами большого файла и двумя короткими PDF — **1587,07 с**.

| Случай | Время OCR | Принято task / POST | Максимум одновременно started | Страницы |
| --- | --- | --- | --- | --- |
| 173 страницы, concurrency=1 | 949,51 с (15:49,51) | 9 / 9 | 1 | 173/173 |
| Тот же файл, concurrency=2 | 501,55 с (8:21,55) | 9 / 9 | 2 | 173/173 |
| Два разных PDF, concurrency=2 | 135,02 с | 2 / 2 | 2 | 6/6 + 8/8 |

Для большого файла ускорение **1,893×**, сокращение времени **47,18%**. Это один
парный замер общего OCR, включая локальную подготовку окон/HTTP/запись checkpoints,
в существующем shared-окружении. Он не является обещанием постоянных 2× или замером
всего последующего VLM-анализа. Медиана наблюдаемого выполнения task — 87,234 с
при одном окне и 87,328 с при двух; ускорение получено перекрытием работ, а не
отключением OCR/таблиц. Медиана наблюдаемой очереди — 0 мс с оговорённой точностью
client polling; это не утверждение о точном серверном времени постановки/старта.

Все страницы/исходные physical identifiers возвращены ровно один раз и в исходном
порядке. Каждый повтор из кеша дал идентичный результат без нового POST.
171 из 173 хешей полного OCR-контекста совпали. На страницах 40 и 101 различается
только одна координата `bbox.l`: максимум 0,00001715. Сверены все 346 исходных
контекстов двух прогонов; текст, таблицы и остальные поля кроме позиции блока
совпали на **всех 173 страницах**. В повторяемый live-тест добавлена автоматическая
проверка такого содержимого: изменение текста/ячейки не скрывается допуском геометрии.
Частная детализация — `reports/local/ocr-layout-differences.json` и
`reports/local/ocr-content-comparison.json`, без печати текста документов.

Реальная проверка использует локально предоставленный владельцем `DOCLING_API_KEY`,
SSH — только для TCP-туннеля к уже существующему Docling. Серверные секреты не выносятся.
Для baseline и параллельного варианта используются разные новые кеши, один PDF
на 173 страницы, окно 20, одинаковые OCR/table options и polling 1 с.
Отдельно одновременно обрабатываются два исходных PDF на 6 и 8 страниц.
Пересечение статусов started подтверждает выполнение, а не только два открытых HTTP.
Частная матрица `reports/local/ocr-concurrency-live.json` содержит времена, counters
и OCR-хеши без текста документов и ключей. Нативные OCR checkpoints остаются в
игнорируемом private temp-каталоге. Это OCR-замер, не повтор VLM-классификации/разделения.

После замера Docling healthy, restart count 0, OOMKilled=false. За контрольные
45 минут в его журнале наблюдались 107 `OSD failed`, 214 `Invalid resolution 0 dpi`,
178 `Too few characters`. Это агрегат shared-журнала, не атрибуция конкретным task.
Предупреждения не стали отказом этих OCR-прогонов. Клиентский параллелизм не меняет
Tesseract/OSD/DPI: их точность относительно исходного изображения требует отдельного
исследования; здесь подтверждено сохранение содержания относительно последовательного OCR.

## Файлы

- Изменены `app/infrastructure/docling.py`, `docling_tasks.py`: очередь окон,
  semaphore, короткий POST lock, single-flight, отмена/запись и метрики.
- Изменены `app/core/settings.py`, `.env.example`, `compose.yaml`: один новый
  несекретный параметр с одинаковым приоритетом локально и в Docker.
- Добавлены `tests/test_docling_concurrency.py`, `test_live_docling_concurrency.py`;
  дополнен `tests/test_configuration.py`.
- Обновлены `AGENTS.md`, `README.md`, `docs/architecture.md`, `docs/operations.md`
  и этот отчёт. Скрипты запуска/проверок и зависимости не менялись. Удалённых файлов нет.
  Commit/push не выполнялись.

## Обновление и повторение

Windows, видимые логи:

```powershell
.\ops\check-quality.ps1
git diff --check
git diff --stat
git add -- .env.example AGENTS.md README.md compose.yaml app/core/settings.py app/infrastructure/docling.py app/infrastructure/docling_tasks.py docs/architecture.md docs/operations.md tests/test_configuration.py tests/test_docling_concurrency.py tests/test_live_docling_concurrency.py reports/ocr-window-concurrency-2026-11-18.md
git commit -m "Process Docling OCR windows with bounded concurrency"
git push origin main
```

Linux, выполняет владелец:

```bash
cd ~/projects/document-intake-ai
git status --short
git pull --ff-only
bash scripts/test.sh
bash scripts/up.sh
# Отдельный тяжёлый замер, с локальными examples и ключом своего Docling:
DOCLING_BASE_URL=http://127.0.0.1:5001 bash scripts/test.sh --live -s tests/test_live_docling_concurrency.py
```

`up.sh` пересобирает/recreate только собственные компоненты и выполняет существующие
readiness/smoke/скачивание; новой миграции и новых секретов нет. Для контрольного
последовательного deployment: `DOCLING_WINDOW_CONCURRENCY=1 bash scripts/up.sh`.
При следующем обычном запуске действует каталог/окружение этого запуска.

Вручную после Ctrl+F5 открыть `http://192.168.55.3:8084/`, загрузить 173 страницы,
проверить рост OCR-счётчика до 173/173, отмену и новый пакет. После OCR проверить
состав/обычное подтверждение диапазонов, завершение извлечения, JSON, оригинал
и PDF каждого блока. Повторить пачку из двух файлов: физические номера внутри
каждого источника и исходный порядок не должны смешиваться. В worker logs видны
`ocr_task_timing`, очередь/выполнение и исход ожидания без документов/ключей.
