<!-- reports/processing-panel-2026-11-17.md -->
# Режимы разделения и единая панель обработки

Основа: `main`, `785273fa682c31a088ac2ebdd31fc0b2bde43451`, remote совпадает.
Проверены актуальные shared-инструкции (`839d419`), включая полный Frontend Guidelines.
Из `contract-diff-search` изучено поведение панели фактических операций; оформление
Document Intake AI сохранено. На сервере HEAD `785273f`, `/health/live` — 200,
частный исправленный судебный эталон присутствует. Серверный код и контейнеры
агент не изменял; commit/push не выполнял. Старый stash сохранён.

## Поведение

Вместо двух переключателей — три вертикальные радиокарточки с утверждёнными текстами.
По умолчанию выбран review: `auto_split=false`, `skip_segmentation=false`.
Умолчание одинаково в Web, `/jobs`, `/uploads`, transport helper, application и Job.
Прежнее автоматическое умолчание намеренно заменено по новому ТЗ; AGENTS и документация
обновлены. Сохранённые задания продолжают использовать свои явно записанные flags.

Для 1С/Telegram: загрузить → дождаться `awaiting_confirmation` → получить
`GET .../segmentation` → при необходимости сохранить правки через `PUT` с текущей
`expected_revision` → подтвердить `POST .../segmentation/confirm` → дождаться JSON.
До подтверждения нет извлечения и `/result` возвращает 409. Повторный `/start`
не обходит проверку. Явный `auto_split=true` сохраняет Auto, `true/true` запрещено.
Skip сохраняет существующее поведение: каждый файл — документ, ручной состав требует
подтверждения. Операционные smoke и матрица точности Auto запрашивают его явно.

Карточки используют одно выбранное значение; преобразование в API flags находится
в `modes.js`. Ручная правка выбирает Skip по серверному ACK. Предупреждение перед
заменой ручного плана сохранено, отказ возвращает прежний режим и состав.

Постоянная панель между загрузкой и редактором содержит запуск, подтверждение,
отмену, новый пакет и экспорт. Промпты и TTL остаются в шапке. Экспорт появляется
после получения готового JSON. Сохраняются редактор, три колонки, лента, жесты и Undo.

В PostgreSQL JSON-снимке задания сохраняются `work_progress.ocr`,
`work_progress.extraction` с `{completed,total}` и последний `work_stage`.
Адаптер сообщает только завершённые OCR-страницы, включая проверенный кеш;
после подтверждения кеш не отображается как заново нераспознанные страницы.
Извлечение считает проверенные документы. Превью и проверки соседей имеют отдельные
существующие счётчики. Snapshot восстанавливает их при reload/reconnect, SSE и polling
используют одно представление. Неизвестный прогресс не имеет процента.
Пропущенные этапы отмечены как пропущенные, неопределённые границы — для проверки.
В журнале человекочитаемые события; сырые OCR/ответы модели в панель не передаются.

Схема результата **JSON 1.0**, маршруты, алгоритм границ, OCR-параметры и извлечение
не изменены. Дополнен только статус/snapshot и события прогресса. Новых секретов,
настроек, миграций или entrypoint-скриптов нет. Требуется обычная пересборка API/worker.

## Проверки

- Штатный Windows `ops/check-quality.ps1 -Browser`: **405 passed, 7 skipped,
  coverage 84%**. Ruff, форматирование и `git diff --check` — PASS.
- Окончательный Linux-прогон в одноразовом локальном контейнере без сети:
  **396 passed, 16 skipped, coverage 83%**, Ruff/форматирование — PASS.
  Приватные PDF в контейнер не передавались; Chromium проверен на Windows.
- Chromium: три режима и их payload, ручная правка/отказ смены режима, подтверждение,
  отдельные счётчики OCR и реквизитов после reload при отключённом SSE, явный Auto,
  пропуски этапов, ошибка/неизвестный прогресс, отмена, новый пакет, повтор файла,
  защита от поздних ответов, весь редактор, 501 страница.
- 173 настоящие страницы синтетического PDF: 172 перехода, 171 ответ и один
  неопределённый; панель показывает проверку состава и сохраняется после reload.
  Это проверка интерфейса/конвейера с doubles, не точность реальной VLM.
- Реальный `суд_ув.pdf` через новый локальный REST API и существующие Docling/VLM:
  **87/87**, `completed_with_warnings`, 418,59 с. До confirm — HTTP 409, затем
  confirm ревизии 3 — HTTP 202. Все пять PDF проверены по SHA-256, количеству и
  пиксельному соответствию страниц. Итоговые счётчики OCR 8/8, документы 5/5.
  Собственные PostgreSQL/RabbitMQ не подменяются этим прогоном: локальный repository
  использует SQLite, доставку тестовый runner; внешние OCR/VLM настоящие.

Ранние прогоны выявили устаревшие тестовые ожидания старых кнопок и любого OCR-события;
они обновлены под новый контракт. Таймаут пар отдельно повторён без конкурирующих
тяжёлых прогонов; ограничение и проверки неизвестных переходов не ослаблялись.
Внутренние логи `.cache/modes-panel-*`, частная матрица
`reports/local/adjacent-court-facts.json`; они исключены из Git.
Скриншоты синтетического UI находятся в `tmp/processing-*.png`.

Точность Auto в прежних больших примерах не исправлялась:
[ранее измеренные ограничения](adjacent-ocr-2026-11-16.md) сохраняются.
Общий `--live` строго проверяет эти расхождения и может завершаться FAIL;
проверки не ослаблены ради нового UI.

## Команды владельца

Windows:

```powershell
.\ops\check-quality.ps1 -Browser
git diff --check
git diff --stat
git add -- AGENTS.md README.md app docs tests reports/processing-panel-2026-11-17.md
git commit -m "Add review defaults and unified processing panel"
git push origin main
```

Linux после push:

```bash
cd ~/projects/document-intake-ai
git pull --ff-only
bash scripts/test.sh
bash scripts/up.sh
curl -fsS http://192.168.55.3:8084/health/ready
uv run --frozen pytest tests/test_live.py::test_real_court_correspondence_and_downloads --live -v
```

Полная матрица точности: `bash scripts/test.sh --live`. Команды показывают логи;
перенаправление пользователь добавляет при необходимости самостоятельно.

Руками открыть LAN UI: выбран первый режим, управление собрано в постоянной панели.
Загрузить судебный PDF, запустить анализ, увидеть пять групп и ожидание подтверждения;
экспорт пока скрыт. Изменить/подтвердить границы, увидеть извлечение и скачать JSON/PDF.
Проверить reload во время OCR и ожидания, отмену и новый пакет, повтор того же файла.
В Auto подтверждение пропускается только при определённых границах; в Skip этап границ
отмечается пропущенным. Для 173 страниц счётчик переходов равен 172.

## Файлы

Удалённых файлов нет. Полный список новых и изменённых файлов приведён ниже.

<!-- file-list -->

### ????? (7)

- `app/web/static/css/blocks/processing.css`
- `app/web/static/js/features/modes.js`
- `app/web/static/js/features/processing.js`
- `app/web/templates/components/processing.html`
- `reports/processing-panel-2026-11-17.md`
- `tests/test_processing_browser.py`
- `tests/test_processing_progress.py`

### ?????????? (32)

- `AGENTS.md`
- `README.md`
- `app/application/analysis.py`
- `app/application/exports.py`
- `app/application/intake.py`
- `app/domain/jobs.py`
- `app/infrastructure/database.py`
- `app/infrastructure/docling.py`
- `app/operations.py`
- `app/presentation/http/routes.py`
- `app/web/static/css/blocks/boundaries.css`
- `app/web/static/css/style.css`
- `app/web/static/js/features/analysis.js`
- `app/web/static/js/features/boundaries.js`
- `app/web/static/js/features/results.js`
- `app/web/templates/base.html`
- `app/web/templates/components/boundaries.html`
- `app/web/templates/index.html`
- `docs/API.md`
- `docs/operations.md`
- `tests/correspondence_support.py`
- `tests/live_support.py`
- `tests/test_adjacent_migration.py`
- `tests/test_adjacent_pairs.py`
- `tests/test_api.py`
- `tests/test_backpressure.py`
- `tests/test_boundary_api.py`
- `tests/test_browser.py`
- `tests/test_manual_plan.py`
- `tests/test_new_packet_browser.py`
- `tests/test_preview_browser.py`
- `tests/test_worker.py`
