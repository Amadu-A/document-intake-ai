<!-- reports/windows-checkpoint-cancellation-2026-11-26.md -->
# Windows: отмена checkpoint и восстановление задания

База: main/remote `3dbc32d37d8d19d114e7872861173703e1edab7f`; рабочая копия в начале чистая.
Актуальные shared guidelines сверены: содержимое совпало с прочитанным ранее.
По присланному серверному логу: 632 passed, 25 skipped; развёртывание и readiness прошли.
Агент не менял сервер, shared-инфраструктуру, RabbitMQ/Docling/VLM или секреты.
Commit/push/deploy не выполнялись.

## Причина и исправление

Присланный Windows-прогон: 634 passed, 22 skipped, один сбой
test_restart_reuses_checkpoints_ids_and_monotonic_unique_progress, SQLite database is locked.
Одиночный исходный прогон прошёл: ошибка зависела от момента отмены.
Контролируемое пересечение записей воспроизвело SQLITE_BUSY на временной БД.
Замены прямой записи lease на штатную транзакцию оказалось недостаточно: повторный
прогон выявил тот же lock при BEGIN IMMEDIATE. Безопасная трассировка показала отмену
SELECT внутри начатой write-транзакции и инвалидирование соединения с открытой транзакцией.
Содержимое документов, SQL-параметры и секреты в трассировку не включались.
Ограничение одного SQLite writer описано в [официальной документации](https://www.sqlite.org/isolation.html).

- Database.checkpoint использует единое ожидание начатой записи для всех вызывающих use-cases:
  одиночная и повторная отмена дожидаются завершения её транзакции, затем передают
  CancelledError вызывающему коду. Ошибка фоновой записи извлекается; задача не остаётся в фоне.
- Repository по-прежнему проверяет fence, lease и deadline внутри транзакции.
  После отзыва fence поздняя публикация запрещена; сетевые запросы под SQL lock не переносятся.
- В тесте восстановления lease изменяется через ту же очередь записей repository.
  Добавлен вариант с незавершённым конкурентным checkpoint; прежние проверки ID,
  fingerprint, монотонности, переиспользования VLM и запрета старого worker сохранены.
- Добавлены отдельные проверки реальной SQLite-транзакции при одной/двух отменах,
  следующей записи и отозванном fence. Рабочие таймауты, retries, ACK, лимиты, prompts и JSON не менялись.

## Проверки

Окончательный production-код, единый Database.checkpoint:

- Windows Python 3.12.15, штатный ops/check-quality.ps1 -Browser: 638 passed,
  22 skipped, 651,48 с, покрытие 88%; Ruff и git diff --check прошли.
- Целевые проверки checkpoint/пула/Pairwise: 58 passed. Пять независимых повторов
  восстановления — 10 случаев passed, включая конкурентный checkpoint.
- Настоящие отдельные RabbitMQ/PostgreSQL на собственном новом образе: 12 passed,
  533,28 с; SIGKILL, ACK timeout, publisher-confirm/connection faults и recovery сохранены.
- Linux Python 3.12.16, полный Ruff/pytest --browser: 638 passed, 22 skipped,
  859,68 с, покрытие 88%, без предупреждений pytest и необработанных исключений.
  Локальные частные примеры включены. В обоих полных прогонах 20 пропусков относятся
  к opt-in live, ещё два — к противоположной ОС. Двенадцать реальных delivery-тестов
  выполнены отдельно; остальные opt-in live не заявляются выполненными.

Защита находится в repository: Pairwise также вызывает checkpoint напрямую, включая
случай бюджета стадии 0,03 с. Analyze не изменён. Первичная проверка Linux выявила этот
путь, что потребовало общего исправления вместо защиты только application-callback.
Последующий Linux-прогон прошёл 637 проверок, но превысил тестовые 5 с до первого VLM
в сценарии трёх пакетов: предварительно создаются девять PDF в subprocess. В тестах
выделен общий бюджет подготовки 30 с; проверки остановки остаются 5 с, рабочие таймауты
сервиса не увеличены. Windows полностью прошёл с прежними более короткими ожиданиями;
после изменения только тестового ожидания целевые регрессии снова прошли: 58 passed за 59,22 с.

Логи окончательного кода: .cache/windows-lease-final-quality.log,
.cache/windows-lease-final-target.log, .cache/windows-lease-final-delivery.log,
.cache/windows-lease-final-repeat-1.log ... -5.log, .cache/windows-lease-verified-linux.log.
Linux проверен в собственном
локальном Docker без сети и .env; examples подключены только для чтения. Компоненты
штатной проверки (uv sync, Ruff, pytest --browser с coverage) выполняются напрямую,
git diff --check — в рабочей копии Windows, поскольку публичный архив не содержит .git.
Проверки пула используют synthetic VLM; это не новый замер точности/скорости shared-VLM.
Локальные диагностические логи .cache/windows-lease-*.log игнорируются Git.

## Команды владельцу

```powershell
.\ops\check-quality.ps1 -Browser
git diff --check
git add -- app/infrastructure/database.py tests/test_extraction_concurrency.py tests/test_checkpoint_cancellation.py reports/windows-checkpoint-cancellation-2026-11-26.md
git diff --cached --stat
git commit -m "Drain checkpoint transactions before worker cancellation"
git push origin main
```

Перед обновлением дождаться активных пакетов либо осознанно отменить их.
На сервере через ssh aistation:

```bash
cd ~/projects/document-intake-ai
git status --short
git pull --ff-only
bash scripts/test.sh --browser
bash scripts/up.sh
curl --fail --show-error http://127.0.0.1:8084/health/ready
```

Дополнительная проверка настоящих изолированных RabbitMQ/PostgreSQL:

```bash
bash scripts/test.sh --live -s tests/test_delivery_live.py tests/test_ack_timeout_live.py tests/test_extraction_delivery_live.py
```

Руками: загрузить пачку, утвердить несколько блоков, дождаться результата и скачать
JSON/PDF/ZIP/XLSX; проверить состав и порядок. В другом пакете отменить извлечение,
убедиться в статусе «Отменено» и запустить новый пакет. Reload/история сохраняют статус.
Для этой правки не нужны миграции, новые секреты или изменения общих сервисов.
