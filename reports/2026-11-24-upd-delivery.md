<!-- reports/2026-11-24-upd-delivery.md -->
# Счёт-фактура и надёжная доставка — 24.11.2026

База: `main`, локальный HEAD и проверенный remote
`dc74690fa7ddb3c1b10f1e06ec65cb5887793a82`. Commit/push не выполнялись.
Актуальные LLM Context, Engineering Guidelines, Infrastructure Instructions и
services.yaml shared_infrasktructure проверены. Сервер использован только для
проверок runtime и вызовов существующих Docling/shared-VLM через разрешённый SSH.
Код сервера, shared конфигурация, пользователи/vhosts брокера и credentials не менялись.

## Доставка: воспроизведение до изменения

Настоящий отдельный Docker RabbitMQ 4.1, PostgreSQL 16 и production worker с текущим
исходным кодом. Только длительная бизнес-операция заменена управляемой паузой;
AMQP, outbox, claim, heartbeat, lease, recovery и consumer ack настоящие.
Стенд имеет случайный префикс `document-intake-delivery-`, собственную сеть,
loopback-порты, volumes и случайные secrets; общий стек не используется.

До production-правки один worker удерживал первое задание, второе ожидало в очереди.
За 38 секунд ready вырос с 1 до 2 при одном unacked. Причина: подтверждённая
публикация оставляла outbox и откладывала следующую отправку на 30 секунд.
Воспроизведение завершилось ожидаемым FAIL до изменения приложения.
Локальный журнал: `.cache/delivery-before.log`.

## Изменение и повтор аварийных сценариев

После publisher confirm outbox удаляется транзакционно только при совпадении
fence с поколением, прочитанным вместе с сигналом одним SQL-снимком. Если confirm
не получен или запись receipt не закоммичена, сигнал сохраняется для повтора.
Поздний confirm прошлого этапа/lease не удаляет новый сигнал того же job_id.
Изменены только методы repository и вызов публикации в собственном worker;
новых миграций и изменений RabbitMQ adapter/конфигурации нет.

At-least-once сохранён. Дубликаты при неопределённом confirm/commit допустимы;
exactly-once не заявляется. Persistent delivery, durable queue, mandatory,
publisher confirms, ручной consumer ack, prefetch, heartbeat и fencing сохранены.
Publisher confirm и consumer ack решают разные задачи: [RabbitMQ confirms](https://www.rabbitmq.com/docs/confirms),
[RabbitMQ reliability](https://www.rabbitmq.com/docs/reliability).

| Реальный изолированный сценарий | Результат повторного набора |
| --- | --- |
| Тот же backlog более 30 секунд | ready `1 → 1`, unacked `1`, оба задания завершены |
| Потерян настоящий AMQP Basic.Ack от брокера в TCP proxy | outbox сохранён; повтор доставлен; бизнес-claim один |
| Соединение с брокером потеряно, затем broker restart | confirmed и pending задания сохранены и завершены |
| Docker SIGKILL worker, restart PG/RabbitMQ/worker | оба задания завершены; crashed attempt=2, fence вырос; старый writer отклонён |

Итог: **4 passed за 170,12 с**, `.cache/delivery-faults-last.log`.
Последний сценарий также принудительно дожидается истечения lease до старта worker.
Один промежуточный повтор остановился по timeout ожидания второго задания.
В тесте устранена зависимость искусственных пауз от порядка повторной доставки:
после restart обеим операциям разрешено завершиться, поскольку FIFO не требуется.
Production механизм восстановления из-за этого timeout не менялся.
Дополнительно repository-тесты проверяют rollback после SQL DELETE до commit,
повтор receipt и поздний confirm подготовки после постановки анализа.

## Частный счёт-фактура

`examples/УПД.pdf` и четыре исходных JSON сохранены без изменений. SHA-256 PDF
проверяется по приложенной матрице. Ни имя, ни хэш, ни конкретный номер документа
не используются в production как семантическое исключение.

Существующий extraction-v6 дополнен общими правилами различения счёта-фактуры,
УПД, счёта на оплату и накладной, собственных номера/даты, ролей продавца/покупателя,
печатных сумм и источников фактов. Для счёта-фактуры проверяется роль D03 `tax`.
Новый промпт или параллельная функция именования не создавались.

Классификатор определяет технический корректирующий угол. Существующий Docling
читает повёрнутую копию блока, vision — соответствующий PNG и области из него.
Оба пути сборки OCR-окон учитывают угол; кеши/принятые tasks разных углов разделены.
Оригинал, физические PageRef, порядок и экспортируемые PDF не меняются.
Поворот требует дополнительного OCR-прохода только для затронутого блока.

| Проверка исходного PDF | До | После |
| --- | --- | --- |
| Одна последовательная группа страниц 1–2 | PASS | PASS |
| Фактический вид | Ошибочно УПД | Счёт-фактура, D03, tax |
| Строгая матрица из 18 стабильных полей | 16 расхождений | 0 расхождений |
| Printed subtotal, VAT, total, товарная позиция и ИНН/КПП | Не соответствовали полному эталону | PASS |
| Status / JSON 1.0 / JSON 2.0 / Content-Disposition / ZIP basename | Неверный вид/неполные факты | Единое ожидаемое имя |
| PDF и ZIP | Проверено повторно | 2 страницы, SHA-256, исходные пиксели и байты PDF в ZIP совпадают |

Два независимых настоящих Docling/VLM прогона строгого API-теста: **PASS** за
68,14 с и 66,67 с. Проверены собственные номер/дата, стороны, одна позиция,
напечатанные деньги без пересчёта округления, ставка налога, evidence и все выдачи
PDF/ZIP. Логи `.cache/upd-live-accuracy-first.log`, `.cache/upd-live-accuracy-repeat.log`.
Частная подробная матрица — `reports/local/upd-differences.json` (игнорируется Git).
Это подтверждение конкретного regression case, не гарантия безошибочности VLM
для произвольного скана. Диагностика неполного распознавания остаётся.

**Утверждённое исключение владельца:** без обслуживаемой организации
`one_c.organization=null`, `direction=null`, `one_c.counterparty=null`.
Иллюстративные JSON с продавцом в counterparty не переписаны. Публичные схемы
1.0/2.0, REST и логика именования сохранены.

## Общая проверка

- Штатный `ops/check-quality.ps1 -Browser`: Ruff/check/format, git diff --check,
  **566 passed, 13 skipped**, покрытие приложения **87%**.
- Настоящий Chromium на частном PDF: загрузка, proposal, reload, подтверждение,
  скачивание ZIP — PASS. OCR/VLM этого browser-теста controlled; он не выдан за live AI.
- Unit/functional/regression/integration/security/architecture входят в полный набор.
- Linux: полный повтор **546 passed, 33 skipped** за 356,64 с, покрытие **86%**.
  Ruff/check/format — PASS. Текущий публичный код проверен в отдельном контейнере
  без сети, `.env` и private fixtures. Первый прогон: 545 passed, 33 skipped, один FAIL
  старого timing-теста OCR (`maximum=1` вместо 2 при задержке всего 10 мс).
  Задержка заменена барьером одновременного старта; текущий Windows-набор OCR:
  **16 passed**. Production лимит OCR не менялся.
- Обычный stderr uv/Docker во внешней PowerShell 5 обёртке перенаправления может
  выглядеть как `NativeCommandError`; это не ошибка pytest. Контрольный native exit
  успешного uv со stderr равен 0. Команды владельцу ниже выводят логи напрямую.
- Общие live-проверки остальных документов и замер 173 страниц в этой доработке
  **NOT_RUN**. Отсутствующие private fixtures/Linux browser и не включённые live
  тесты явно отмечаются skipped/NOT_RUN, не считаются успешной точностью AI.

## Команды владельцу

Windows, из каталога проекта, без автоматического скрытия логов:

```powershell
.\ops\check-quality.ps1 -Browser
git diff --check
git status --short
git diff --stat
git add -- app tests docs prompts/extraction-v6.txt reports/2026-11-24-upd-delivery.md
git diff --cached --stat
git commit -m "Fix reliable delivery and rotated invoice recognition"
git push origin main
```

Пути `app`, `tests`, `docs` содержат только изменения этой доработки по проверенному
status; перед add просмотреть diff. `examples`, `.env`, `.cache`, private reports
не добавляются. Если PowerShell запрещает ps1, разовый обход политики:
`powershell -NoProfile -ExecutionPolicy Bypass -File .\ops\check-quality.ps1 -Browser`.

Ubuntu после пользовательского push:

```bash
cd ~/projects/document-intake-ai
git pull --ff-only
bash scripts/test.sh
bash scripts/up.sh
docker compose --env-file .env.example --env-file .env ps
```

Точечный настоящий PDF-тест при наличии всех пяти private fixtures и ключа Docling:
`bash scripts/test.sh --live tests/test_upd_example.py`.
Аварийный стенд: `bash scripts/test.sh --live -s tests/test_delivery_live.py`;
нужны локальный Docker и готовые RabbitMQ/PG/app images согласно docs/operations.md.
Локально использован прежний образ окружения `document-intake-ai:adjacent-20261116`
с текущими app/prompts, смонтированными read-only, через `--delivery-image`.

## Ручная приёмка

Открыть `http://192.168.55.3:8084/`. Для сохранённого пользовательского промпта:
«Промпты» → извлечение → восстановить базовый → сохранить. Автоматически overrides
и snapshots запущенных заданий не переписываются.

Загрузить исходный `УПД.pdf` в режиме проверки предложения, дождаться одной группы
1–2, сохранить/подтвердить её. Убедиться в виде «Счёт-фактура», собственных номере
и дате по эталону, ИНН/КПП обеих сторон, одной позиции и трёх напечатанных суммах
без исправления округления. «Разрезать и скачать» должно выдать ZIP с одним PDF
в папке `doc_1` и ожидаемым полным именем. Открыть PDF: оба исходных листа на месте.
Reload/история и повторные `/result`, `/extractions`, download не меняют результат.
