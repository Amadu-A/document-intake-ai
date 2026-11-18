<!-- reports/archive-folders-2026-11-18.md -->
# Папки документов в скачиваемом архиве

ZIP теперь содержит:

```text
doc_1/
  Входящее_письмо_42_2026-11-18.pdf
doc_2/
  Счёт.pdf
```

Папка соответствует глобальному номеру блока пачки. В PDF внутри папки префикса
`doc_N_` нет; отсутствующие номер/дата пропускаются. Одинаковые базовые имена
разных документов сохраняются в разных папках без конфликта при распаковке.
Пути ZIP используют прямой слеш и прежнюю нормализацию недоверенных значений.
Отдельные download/JSON имена сохраняют прежний префикс номера блока.

Domain формирует basename и путь ZIP из тех же проверенных фактов. Exports
передаёт полный путь entry существующему Storage port. Он уже входит в fingerprint
кеша, поэтому прежний плоский архив не возвращается после обновления.
PDF, порядок страниц, owner/capability/TTL, SHA-256 и атомарная сборка не менялись.
Нет новых настроек, миграций, зависимостей или повторных OCR/VLM-вызовов.

Начальные main/HEAD и origin/main — `34fb85c9ce7a393879840776e19da7528f9b6100`,
рабочее дерево чистое. Актуальные shared LLM Context/Engineering Guidelines
проверены, SHA `f42ede53`/`5e6a9c17`. Сервер/shared services и секреты не менялись.

Проверки:

- Unit: варианты вида/номера/даты и небезопасные значения дают правильный basename
  в папке `doc_N`; прежние отдельные имена сохранены.
- REST/DB/PDF: скачан ZIP, реально распакованы папки, содержимое каждого PDF
  совпадает с отдельной загрузкой; порядок страниц и checksums верны.
- Регрессия одинаковых имён: `doc_1/Письмо.pdf`, `doc_2/Письмо.pdf` содержат
  разные соответствующие страницы. Старый плоский кеш не переиспользуется.
- Chromium: реальное скачивание из UI после утверждения/reload и повтор после
  ошибки содержат новую структуру папок; мышиный перенос продолжает работать.
- Целевой Windows-прогон с Chromium и архитектурой: **15 passed**.

Первый полный Windows-прогон с Chromium: **470 passed, 8 skipped, 1 failed**.
Найден существующий сбой `test_pair_stage_timeout_preserves_unknowns_and_stops_pending_calls`:
coordinator записывал timeout checkpoint до завершения cleanup всех отменённых
workers. Отдельная детерминированная регрессия на старом коде подтвердила запись
при ещё активном sibling. Исправлен `Pairwise.run`: сначала отмена/дренирование
всех workers, затем запись неизвестных переходов; повторная отмена не прерывает
уже начатый cleanup. Бюджеты, решения VLM и требования исходного теста не ослаблены.
После исправления пары/таймауты, ZIP, архитектура и Chromium: **60 passed**.
Покрытие из этого целевого прогона не учитывается: был ошибочно указан файловый
путь вместо имени модуля для pytest-cov; итоговое покрытие берётся из штатного прогона.

Целевой Linux-прогон: **37 passed**, Ruff check/format прошли. Проверены ZIP,
основной REST API, ручной порядок PDF и архитектура в одноразовом локальном
контейнере без сети, .env и приватных примеров. После исправления отмены целевой
Linux-прогон расширен проверками соседних пар: **82 passed**, Ruff check/format
прошли. Окончательный общий Windows `ops/check-quality.ps1` без `-Browser`:
**460 passed, 20 skipped**, покрытие **85%**; Ruff check/format и `git diff --check` прошли.
Chromium проверен отдельно в указанном выше целевом прогоне после исправления.
Пропущены opt-in live/browser и две неприменимые Windows проверки Unix-владельца.
Live OCR/VLM/AMQP/PostgreSQL и серверный deploy для этой поправки не запускались.
Предыдущая доработка и её замеры: [перетаскивание и архив](drag-archive-2026-11-18.md).

Изменены `app/domain/document_names.py`, `app/application/exports.py`,
`app/application/pairwise.py`, `tests/test_adjacent_pairs.py`,
`tests/test_document_archive.py`, `tests/test_drag_archive_browser.py`,
`AGENTS.md`, `README.md`, `docs/API.md`, `docs/architecture.md`, `docs/operations.md`
и отчёты. Удалений нет. Commit/push/staging не выполнялись.

Windows:

```powershell
.\ops\check-quality.ps1 -Browser
git diff --check
git add -- AGENTS.md README.md app/domain/document_names.py app/application/exports.py app/application/pairwise.py tests/test_adjacent_pairs.py tests/test_document_archive.py tests/test_drag_archive_browser.py docs/API.md docs/architecture.md docs/operations.md reports/drag-archive-2026-11-18.md reports/archive-folders-2026-11-18.md
git commit -m "Group archived PDFs and drain timed-out pair workers"
git push origin main
```

Linux после push:

```bash
cd ~/projects/document-intake-ai
git status --short
git pull --ff-only
bash scripts/test.sh
bash scripts/up.sh
curl --fail http://127.0.0.1:8084/health/ready
```

Вручную: на `http://192.168.55.3:8084/` обновить страницу Ctrl+F5, обработать
пачку из нескольких блоков и нажать «Разрезать и скачать». Распаковать ZIP:
проверить папки `doc_1`, `doc_2`, PDF без повторного префикса, отсутствие пустых
частей имени при null и сохранность документов с одинаковыми именами.
Повторное скачивание уже обработанной пачки также должно дать новую структуру.
