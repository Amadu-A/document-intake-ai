<!-- reports/validation.md -->
# Фактические проверки — 03.11.2026

Код реализован в рабочем дереве, commit/push и серверное развёртывание агентом не выполнялись.
Готовность production по Definition of Done **не подтверждена**: нет доступа к
серверному Docker/shared runtime, внешнему Docling key и другой LAN-машине.

| Проверка | Статус | Фактический объём |
|---|---|---|
| Remote/local preflight | PASS | main проекта `72e001f4`, shared `839d4194`, reference `fa67a146`; повторно сверены |
| Ruff check / format | PASS | Все собственные Python-файлы, без правок пользовательских examples |
| Unit/functional/regression/security/architecture | PASS | Вместе с browser: 75 тестов; два opt-in live теста SKIPPED |
| Общее покрытие с ветвлениями | 74% | Windows, Python 3.12.15; непроверенные runtime-ветки не исключены из метрики |
| Реальный Chromium / HTTP | PASS | Два промпта, edit/restore/save, несколько PDF, SSE, JSON export, refresh, viewer/source links, Swagger |
| Visual review | PASS | Desktop 1680px и mobile 390px; локальные screenshots в игнорируемом tmp/ |
| JSON 1.0 / каталог | PASS | Строгая схема, 38 конечных путей, неизвестные значения запрещены |
| Претензия 1 + 4 приложения | PASS, synthetic VLM | Реальный PDF из 6 страниц, order/byte_size/SHA/download проверены; это не live оценка модели |
| TIFF / PNG / JPG / PDF | PASS | Настоящие конвертации; TIFF 3 кадра, PDF 53 страницы, encrypted PDF, заполненное поле PDF |
| VLM/Docling HTTP adapters | PASS, MockTransport | Vision base64, alias/schema/SSE; Docling auth, tables, local→global page 51, partial failure |
| Lease/outbox/recovery/TTL | PASS, SQLite | Устаревший fence/отмена, retry/deadline, реальный цикл reaper, потеря HTTP после commit |
| Alembic DDL | PASS, SQLite | Начальная миграция совпадает с ORM tables/columns; production не использует create_all |
| Compose rendering | PASS | `docker compose ... config --quiet`, каталог + пустой тестовый overlay; daemon не нужен |
| Bash/PowerShell syntax | PASS | `bash -n` для двух скриптов, PowerShell parser |
| Единый PowerShell entrypoint | PASS | `check-quality.ps1 -Browser`: uv sync, установка browser prerequisites, Ruff, format, diff check и pytest; разовый process ExecutionPolicy Bypass |
| Linux one-command deploy | NOT_RUN | Docker daemon серверного aistation недоступен из среды разработки |
| Linux Office / seccomp | NOT_RUN | Linux-контейнер здесь не запускался; DOC/DOCX/TXT и syscall-изоляция требуют runtime приёмки |
| PostgreSQL concurrency | NOT_RUN | SQLite не доказывает реальные row locks/advisory locks PostgreSQL |
| Real AMQP / безопасный bootstrap / consumer | NOT_RUN | Текущий shared RabbitMQ недоступен; source/static checks не заменяют execution |
| Real Docling / shared-VLM | NOT_RUN | Нет доступа к runtime/разрешённому Docling key |
| Пять пар: фактическая точность VLM | NOT_RUN | Источники просмотрены и матрица готова; реальные expected/actual пока не получены |
| LAN download из другой машины | NOT_RUN | Нужна проверка владельцем после up.sh и разрешённого firewall |

Промежуточные ошибки исправлены: Windows temp permissions в sandbox обойдены
локальным pytest basetemp; браузерные тесты используют отдельные SQLite connections;
Swagger обновлён до bundled v5 с OpenAPI 3.1; значения PDF widgets запекаются в
нормализованный PDF с сохранением оригинала; post-commit cancellation сохраняет оригиналы.
Полный прогон после исправлений: `75 passed, 2 skipped`, 16.77 секунды.
Это длительность локальных тестов, а не производительность VLM.
Единый PowerShell-прогон прошёл целиком. Финальный запуск использовал локальные
pytest basetemp/cache внутри workspace из-за прав sandbox; предупреждений не было.

Dependency lock: `uv.lock`. Образы закреплены версиями; Python/PostgreSQL tags
сверены с [официальным каталогом Python](https://github.com/docker-library/official-images/blob/master/library/python)
и [PostgreSQL](https://github.com/docker-library/official-images/blob/master/library/postgres).
Manifest всех трёх закреплённых images (Python, PostgreSQL, uv) проверен: PASS.
Сборка и pull images ещё не выполнялись. Контракты адаптеров сверены с
[Docling Serve usage](https://github.com/docling-project/docling-serve/blob/v1.38.2/docs/usage.md)
и [vLLM structured outputs](https://docs.vllm.ai/en/latest/features/structured_outputs/).

Перед эксплуатационной приёмкой владелец выполняет `bash scripts/up.sh`, затем
`bash scripts/test.sh --live`. Команда запуска сама проверяет настоящие OCR, vision/JSON,
AMQP/consumer, PostgreSQL и HTTP pipeline/download по внешнему адресу; при неуспехе
не печатает READY. Реальная матрица сохраняется в `reports/local/live-comparison.json`.
Ручной сценарий приведён в `docs/operations.md`.
