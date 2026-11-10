<!-- reports/boundary-editor-audit.md -->
# Аудит режимов и редактора — 10.11.2026

Начальное состояние: local/GitHub/server main `5c0d1f440c5cfa0789723a57f3bc1622499841d7`.
Shared main `839d419432fc25bc988e5871f7e53f6d282a2e77`; применимые инструкции проверены.
На сервере read-only подтверждены healthy API, worker, PostgreSQL, Docling, VLM и RabbitMQ.
Серверный код и shared stack агент не меняет; новые частные reference игнорируются Git.

| Область | До изменения | Требуемое изменение |
|---|---|---|
| Режим | Только автоматический VLM | Legacy auto, review, source files/manual |
| Превью | После конвертации всего файла; `/uploads` не запускает worker | Отдельная queued preparation без OCR/VLM, прогрессивные страницы |
| План | Один VLM manifest | Proposal/draft/approved, ревизия, owner, атомарный lock |
| Worker | Только `running` до терминального результата | Освобождение lease в `awaiting_confirmation` и после подготовки |
| TTL | Общий deadline и час от upload | Независимые queue/preview/processing, час ожидания после preview |
| Страницы | Все URLs в каждом snapshot, все thumbnails в DOM | Пагинация, лёгкий snapshot, виртуальная лента |
| Лимиты | 100 МиБ/500 страниц на всю пачку | Per-file 400 МиБ/1000, отдельные batch guards |
| Большой VLM | Один глобальный ответ со всеми page refs | Ограниченное многоуровневое согласование стыков |
| JSON | Контракт 1.0 | Сохраняется; режимы/исключения только в статусе/manifest |

Все существующие таблицы уже хранят агрегат в JSON и status в строке без enum/check.
Новые поля имеют обратно совместимые defaults; миграция DDL не нужна. Новые
переходы используют существующие row/admission locks, журнал и outbox.
Отдельные проверки PostgreSQL concurrency и больших workload выполнены;
результаты и ограничения — в [отчёте проверок](boundary-editor-validation.md).
Healthy работающего старого стека не считается приёмкой нового кода.
