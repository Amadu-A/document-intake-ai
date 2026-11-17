<!-- reports/adjacent-ocr-2026-11-16.md -->
# Последовательное разделение по соседним OCR

Реализован новый алгоритм и удалён старый механизм. **Точность на реальных больших
пачках не принята**: есть неизвестные переходы и расхождения с подтверждёнными границами.
Это не скрывается за успешным HTTP, готовностью контейнеров или синтетическими тестами.
Коммит, push и развёртывание нового кода на сервере не выполнялись.
Итоговая сверка рабочего дерева и remote выполнена 17.11.2026.

Исходная точка: main `55c3ca5bbdb404e0bfb8d781261f76d4eb594b93`;
remote main совпадает. Проверены актуальные инструкции shared_infrasktructure,
main `839d419432fc25bc988e5871f7e53f6d282a2e77`. Последнее решение владельца заменяет
старое требование дальнего VLM-согласования и постраничного запасного пути.

## Изменения

OCR всей пачки предшествует N−1 независимым текстовым запросам каждого исходного файла.
Ответ содержит только строгий boolean `solution`. Изображения на стадии границ
не передаются, дополнительные VLM-согласования не выполняются. Межфайловое
автоматическое объединение запрещено. Ручные несмежные/межфайловые группы сохранены.

Параллелизм по умолчанию 4; сохраняются попытки, ответы, адреса, время и отпечаток
OCR/промпта/версии каждой пары. Restart не повторяет успешные ответы и не обнуляет
исчерпанный бюджет. Полный OCR используется для сравнения; прежнее ограничение
vision-контекста реквизитов сохранено отдельно. Ошибки и пустой OCR остаются неизвестными
переходами, дают `vlm_partial`/`awaiting_confirmation`, не превращаются в false.
После явного утверждения используется ровно утверждённый состав без новых сравнений.
Все успешные ответы auto заканчиваются без подтверждения.

Базовый промпт — segmentation-v4 из ТЗ. Старый сессионный segmentation сбрасывается,
пользовательский extraction сохраняется. Новые задания получают immutable snapshots.
JSON 1.0, исходники, PDF, SHA-256, TTL, API-пути и параметры сохранены.

Отмена отзывает fence немедленно; worker проверяет отзыв каждые 250 мс и прерывает
корутины/дочерние задачи. Общие сервисы не останавливаются. Принятая upstream task
может закончиться в самом Docling, если его API не поддерживает удалённую отмену.
Новый пакет доступен всегда: полный сброс без reload, отзыв SSE/запросов/blob URLs,
очистка input/sessionStorage/редактора/результатов и защита от поздних ответов.
При отказе cancel доступны повтор или явное отсоединение с предупреждением.

Миграция 0002 сохраняет готовые результаты и завершает активные старые задания
ошибкой `algorithm_version_changed`; их нужно загрузить заново. `up.sh` перед
миграцией останавливает только собственные API/worker. Секреты и shared services не меняются.
Для SQLite добавлена справедливая очередь записей: многократные checkpoint больше
не вытесняют ожидающего writer до ошибки блокировки. PostgreSQL сохраняет row locks.
Исправлены гонки Undo/ручек границ и ложный останов Windows PowerShell при статусном stderr uv.

## Реальные примеры и точность

Новый код запускался локально с настоящими Docling/shared-VLM через SSH.
Доставка задания выполнялась локальным worker/тестовым циклом, состояние — SQLite.
Это **не** проверка новой версии PostgreSQL/AMQP/LAN deployment на сервере.
Серверный код не редактировался. Ранее работающие контейнеры проекта и зависимости healthy.

Для авторазделения использованы отдельные последовательные эталоны из подтверждённых
`*_cut.json`, скопированные в приватные `*.adjacent.expected.json` до сравнения.
PDF, полные исторические JSON и несмежные эталоны не изменялись.
Исторические [1,8] и [101,103] автоматически не восстанавливаются.
Сравнение не преобразует старый несмежный эталон в новый ответ модели.

Первый холодный прогон:

| Страниц | Логических пар | Строгих ответов | Неизвестных | Верных среди ответивших | Групп: результат / эталон | Точных групп |
|---:|---:|---:|---:|---:|---:|---:|
| 8 | 7 | 7 | 0 | 7 / 7 | 5 / 5 | 5 |
| 50 | 49 | 39 | 10 | 28 / 39 | 39 / 45 | 26 |
| 128 | 127 | 106 | 21 | 75 / 106 | 90 / 95 | 49 |
| 173 | 172 | 150 | 22 | 118 / 150 | 105 / 111 | 61 |

8 страниц — «суд_ув»; 50/128 — префиксы большого исходника; 173 — весь большой PDF.
Во всех четырёх случаях покрытие страниц полное, без повторов/потерь. У большого
исходника неизвестные стыки связаны с непригодным OCR. Успешные booleans также
могут быть семантически неверны: отсутствие технической ошибки не доказывает точность.
Прогоны выполнены в review; на больших пачках источник `vlm_partial`, результата
извлечения до явного утверждения нет. Постраничный запасной сценарий не применялся.

| Страниц | Холодный OCR, с | Холодный полный путь до предложения, с | Отдельный повтор стадии пар с готовым реальным OCR, с |
|---:|---:|---:|---:|
| 8 | 40,297 | 51,719 | 9,359 |
| 50 | 302,187 | 375,562 | 28,204 |
| 128 | 1060,078 | 1327,844 | 171,453 |
| 173 | 1186,391 | 1475,844 | 648,610, полный OCR |

Последний столбец — отдельный реальный повтор VLM-стадии, **не** составляющая того же
холодного прогона. Его нельзя складывать с OCR и выдавать за один измеренный pipeline.
В холодном прогоне 173 страниц стадия VLM дополнительно измерена наблюдением состояния:
249,828 с, разрешение наблюдения 0,5 с. Замер 128 таким наблюдением захватил не весь
этап и не используется как полное время. Вывод о гарантированном ускорении не делается.
На 8 страницах отдельные повторы: concurrency 2 — 9,609 с; 4 — 9,359 с; 8 — 2,328 с.
Малая выборка и разная общая нагрузка не обосновывают повышение default выше 4.

В исходных холодных прогонах 128/173 страниц семь длинных страниц использовали
прежний vision-контекст до 20000 символов. В финальном коде границы получают полный OCR.
Это дополнительно проверено без повторного OCR: для 128 страниц переиспользованы
неизменившиеся ответы, выполнено ровно 14 новых модельных запросов за 57,844 с;
точность осталась 75/106, неизвестных 21. Полный новый прогон пар для 173 страниц
занял 648,610 с: 172 перехода, 150 ответов, 22 неизвестных, **116 верных / 34 неверных**
среди ответивших, 105 групп и 60 точных групп из 111. Ускорение и улучшение точности
не подтверждены. Разные прогоны/контексты в отчёте не смешиваются в один результат.

Из пяти прежних пар четыре совпали с предметной матрицей и проверками PDF.
`4-2591.pdf` дал [1,2,3], [4], [5], [6] вместо одного комплекта 1–6.
Это ответы модели false между соседними актами, а не fallback или ошибка покрытия.
Без контекста основной претензии на каждой паре связь приложений не гарантируется.
Backend не исправляет это объединением по контрагенту/договору. Эталон не подогнан.
Для такого исходника можно явно выбрать skip или проверить/объединить состав в review.

Судебный пример дополнительно прошёл извлечение и скачивание пяти PDF:
[1], [2,3,4], [5,6], [7], [8]. Проверены SHA-256, страницы и пиксельное соответствие.
Из 87 предметных/структурных проверок одна расходится:
`[2,3,4] → requisites.related_entities.court_cases[1].date`.
Отдельного нового эталона фактов конверта [1] и повестки [8] нет; объединённый
исторический JSON не копировался в два новых выдуманных эталона.

Приватные матрицы/замеры: `reports/local/adjacent-benchmarks.json`,
`adjacent-pair-matrix.json`, `adjacent-reference-provenance.json`,
`adjacent-warm-pairs.json`, `adjacent-five.json`, `adjacent-court-facts.json`.
Финальная проверка полного OCR: `adjacent-full128.json`, `adjacent-full-ocr-accuracy.json`.
Они игнорируются Git; документы/OCR/секреты в обычные логи не включены.

## Технические проверки

- Итоговый штатный `ops/check-quality.ps1 -Browser`: **387 passed, 7 skipped, coverage 84%**.
  Расширенная проверка отказа cancel/явного отсоединения затем пройдена отдельно в Chromium.
- После расширения негативных сценариев: 45 проверок пар/диагностики passed;
  затем 70 проверок полного OCR/пар/Docling/HTTP-адаптеров passed.
- После уточнения skip-счётчиков: 28 API/редакторных проверок passed.
- Linux в собственном локальном контейнере: **379 passed, 14 skipped**.
  Новый защитный сценарий переключения partial → Auto после этого проверен в
  итоговом Windows-прогоне и 72 API/парных регрессиях. Результат Linux не выдаётся
  за применение новой миграции к серверной PostgreSQL.
- Chromium проверяет 501 страницу, rate limits/кеш, ручки, Undo, режимы,
  несмежный состав, cancel/reset, повтор файла, поздний ответ, reload и drag/drop после отказа.
- Docker nonroot/read-only, без сети/capabilities: настоящие converter/PDF и
  async OCR HTTP-контракт на double — PASS. Это не real Docling PASS.
- Ruff и форматирование — PASS. JSON 1.0 не изменён. Commit/push не выполнялись.

## Проверка владельцем

Windows:

```powershell
.\ops\check-quality.ps1 -Browser
git diff --check
git diff --stat
git add -- .env.example AGENTS.md README.md app compose.yaml docs migrations ops/check-quality.ps1 prompts scripts/up.sh tests reports/adjacent-ocr-2026-11-16.md
git commit -m "Replace segmentation with adjacent OCR comparisons and fix packet reset"
git push origin main
```

Linux после push владельцем:

```bash
cd ~/projects/document-intake-ai
git pull --ff-only
bash scripts/test.sh
bash scripts/up.sh
curl -fsS http://192.168.55.3:8084/health/ready
bash scripts/test.sh --live
```

`--live` требует собственный evaluation API-ключ и приватные примеры. Он проверяет
точность строго: описанные выше расхождения являются FAIL, а не безопасным проходом.
Команды показывают логи; перенаправление в файл пользователь добавляет самостоятельно.

Ручная проверка: открыть LAN UI, проверить две вкладки промптов; загрузить «суд_ув»
в review и увидеть пять последовательных групп. Проверить progress сравнений,
проблемные стыки «?», объединение/разделение/Undo/несмежный выбор и сохранение после reload.
Утвердить состав, скачать JSON/PDF/исходник. На каждом этапе нажать cancel/«Новый пакет»,
проверить очистку без reload, повтор того же файла и отсутствие старого job после Ctrl+F5.
Для `4-2591` проверить один вручную утверждённый комплект с четырьмя приложениями.
Не считать текущую точность большого исходника принятой.

## Файлы

Полный перечень изменённых, удалённых и новых файлов приводится ниже.

<!-- file-list -->

### Новые (9)

- [app/application/pairwise.py](../app/application/pairwise.py)
- [app/domain/comparisons.py](../app/domain/comparisons.py)
- [migrations/versions/0002_adjacent_ocr.py](../migrations/versions/0002_adjacent_ocr.py)
- [prompts/segmentation-v4.txt](../prompts/segmentation-v4.txt)
- [reports/adjacent-ocr-2026-11-16.md](../reports/adjacent-ocr-2026-11-16.md)
- [tests/test_adjacent_migration.py](../tests/test_adjacent_migration.py)
- [tests/test_adjacent_pairs.py](../tests/test_adjacent_pairs.py)
- [tests/test_new_packet_browser.py](../tests/test_new_packet_browser.py)
- [tests/test_windows_quality.py](../tests/test_windows_quality.py)

### Удалённые (7)

- `app/application/reconciliation.py`
- `app/domain/compact_plan.py`
- `prompts/segmentation-v1.txt`
- `prompts/segmentation-v2.txt`
- `prompts/segmentation-v3.txt`
- `tests/test_distant_evidence.py`
- `tests/test_interleaved_segmentation.py`

### Изменённые (52)

- [.env.example](../.env.example)
- [AGENTS.md](../AGENTS.md)
- [README.md](../README.md)
- [app/application/analysis.py](../app/application/analysis.py)
- [app/application/exports.py](../app/application/exports.py)
- [app/application/inference_schema.py](../app/application/inference_schema.py)
- [app/application/intake.py](../app/application/intake.py)
- [app/application/ports.py](../app/application/ports.py)
- [app/application/prompts.py](../app/application/prompts.py)
- [app/core/observability.py](../app/core/observability.py)
- [app/core/settings.py](../app/core/settings.py)
- [app/domain/boundaries.py](../app/domain/boundaries.py)
- [app/domain/jobs.py](../app/domain/jobs.py)
- [app/domain/segmentation.py](../app/domain/segmentation.py)
- [app/infrastructure/boundary_transactions.py](../app/infrastructure/boundary_transactions.py)
- [app/infrastructure/database.py](../app/infrastructure/database.py)
- [app/infrastructure/docling.py](../app/infrastructure/docling.py)
- [app/web/static/css/blocks/boundaries.css](../app/web/static/css/blocks/boundaries.css)
- [app/web/static/js/features/analysis.js](../app/web/static/js/features/analysis.js)
- [app/web/static/js/features/boundaries.js](../app/web/static/js/features/boundaries.js)
- [app/web/static/js/features/boundary_state.js](../app/web/static/js/features/boundary_state.js)
- [app/web/static/js/features/results.js](../app/web/static/js/features/results.js)
- [app/web/static/js/features/viewer.js](../app/web/static/js/features/viewer.js)
- [app/web/templates/index.html](../app/web/templates/index.html)
- [app/worker.py](../app/worker.py)
- [compose.yaml](../compose.yaml)
- [docs/API.md](../docs/API.md)
- [docs/architecture.md](../docs/architecture.md)
- [docs/operations.md](../docs/operations.md)
- [ops/check-quality.ps1](../ops/check-quality.ps1)
- [scripts/up.sh](../scripts/up.sh)
- [tests/correspondence_support.py](../tests/correspondence_support.py)
- [tests/segmentation_support.py](../tests/segmentation_support.py)
- [tests/support.py](../tests/support.py)
- [tests/test_api.py](../tests/test_api.py)
- [tests/test_backpressure.py](../tests/test_backpressure.py)
- [tests/test_boundary_api.py](../tests/test_boundary_api.py)
- [tests/test_browser.py](../tests/test_browser.py)
- [tests/test_correspondence_api.py](../tests/test_correspondence_api.py)
- [tests/test_correspondence_projection.py](../tests/test_correspondence_projection.py)
- [tests/test_domain.py](../tests/test_domain.py)
- [tests/test_inference_schema.py](../tests/test_inference_schema.py)
- [tests/test_live.py](../tests/test_live.py)
- [tests/test_live_segmentation.py](../tests/test_live_segmentation.py)
- [tests/test_live_support.py](../tests/test_live_support.py)
- [tests/test_observability.py](../tests/test_observability.py)
- [tests/test_ocr_context.py](../tests/test_ocr_context.py)
- [tests/test_preview_browser.py](../tests/test_preview_browser.py)
- [tests/test_primary_date.py](../tests/test_primary_date.py)
- [tests/test_scaling.py](../tests/test_scaling.py)
- [tests/test_segmentation_examples.py](../tests/test_segmentation_examples.py)
- [tests/test_windows.py](../tests/test_windows.py)
