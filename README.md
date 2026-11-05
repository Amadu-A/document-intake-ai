<!-- README.md -->
# Document Intake AI

FastAPI-монолит принимает PDF, DOC/DOCX, TXT, JPG/PNG и многостраничный TIFF.
Docling извлекает OCR/таблицы, shared-VLM самостоятельно определяет регистрационные
документы и приложения, затем классифицирует и извлекает реквизиты.
Результат — JSON **1.0** и отдельный PDF-комплект для каждой регистрации.
1С получает файлы через REST без открытия web. Запись и поиск в 1С сервис не выполняет.

После запуска анализа вопросы и ручное подтверждение не нужны. Некорректный ответ
модели проходит ограниченные повторы; при неуспехе возвращается диагностическая ошибка.
Все данные задания доступны **1 час после завершения**, загрузка без запуска — час от загрузки.

Структура: `app/domain` — контракты и каталог; `application` — сценарии/порты;
`infrastructure` — PostgreSQL, RabbitMQ, Docling, VLM, PDF; `presentation/http` — REST;
`web` — Jinja2/CSS/JS; `prompts` — две базовые версии; `migrations`, `tests`, `reports`.

Для развёртывания нужны Linux, Docker с Compose v2, доступ к Docker daemon,
существующая сеть `ai-shared`, работающие shared RabbitMQ/Docling/VLM и разрешённый
`DOCLING_API_KEY`. Сервис не создаёт и не перезапускает shared-компоненты.

```bash
bash scripts/up.sh
```

Первый запуск атомарно создаёт отсутствующий `.env` с собственными случайными
`DATABASE_PASSWORD`, `RABBITMQ_PASSWORD`, `SESSION_SECRET`, `DOWNLOAD_SECRET`,
`API_KEYS` (`onec`, `telegram`, `evaluation`). Значения не печатаются. Существующий
`.env` не перезаписывается. Добавьте в него разрешённый `DOCLING_API_KEY`, если
ключ ещё не предоставлен через окружение; повторите ту же команду.
Не копируйте весь `.env.example` и не используйте секреты соседних проектов.

Pydantic Settings: `.env.example` → `.env` → environment процесса.
Каталог несекретных параметров находится в `.env.example`.
По умолчанию UI: `http://192.168.55.3:8084/`, Swagger: `/docs`.
Порт `8083` не используется. Firewall должен разрешать `8084` только нужной LAN/VPN;
скрипт firewall не меняет. Web без логина, с изолированной HttpOnly-сессией и CSRF.

Проверки с видимым выводом:

```powershell
.\ops\check-quality.ps1
```

```bash
bash scripts/test.sh
# После развёртывания, с локальными examples и evaluation key:
bash scripts/test.sh --live
```

Chromium автоматически устанавливается при `./ops/check-quality.ps1 -Browser`
или `bash scripts/test.sh --browser`.
На Linux нужны системные библиотеки Playwright. Реальная модель и браузер — разные проверки.
Если Windows запрещает выполнение `.ps1`, разовый запуск без изменения политики системы:
`powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\check-quality.ps1`.

Подробности: [API](docs/API.md), [архитектура](docs/architecture.md),
[эксплуатация](docs/operations.md), [аудит примеров](reports/fixture-audit.md),
[фактический статус проверок](reports/validation.md).
Приватные `examples/` игнорируются для новых файлов; уже отслеживаемые оригиналы не изменялись.
