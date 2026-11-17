// app/web/static/js/features/processing.js
/** Представление серверного snapshot: не хранит отдельную систему состояний задания. */
const names = { preview: "Превью", ocr: "OCR", boundaries: "Границы", confirmation: "Подтверждение", extraction: "Реквизиты", done: "Готово" };
const stages = { preparing_preview: "preview", preview_ready: "preview", docling: "ocr", segmentation_vlm: "boundaries", awaiting_confirmation: "confirmation", extraction_vlm: "extraction" };
const finished = new Set(["completed", "completed_with_warnings", "failed", "cancelled"]);
const statusNames = { uploaded: "Документы загружены", queued: "В очереди", running: "Обработка выполняется", awaiting_confirmation: "Ожидаем подтверждения", completed: "Готово", completed_with_warnings: "Готово с предупреждениями", failed: "Ошибка", cancelled: "Отменено" };

export function renderProcessing(data, { uploading = false, cancelling = false, starting = false, pending = false, confirmed = false, error = null, unavailable = false } = {}) {
  /** Все надписи, действия и этапы выводятся из текущего snapshot и незавершённых UI-команд. */
  const get = selector => document.querySelector(selector);
  const segmentation = data?.segmentation || {}, status = data?.status;
  const success = status === "completed" || status === "completed_with_warnings";
  const mode = data?.mode || {}, counters = data?.work_progress || {};
  const documents = Object.values(data?.document_processing || {});
  const currentDocument = documents.find(p => p.status === "processing");
  const active = stages[data?.work_stage || data?.stage];
  const review = status === "awaiting_confirmation";
  const manual = segmentation.segmentation_source?.startsWith("manual");
  const ready = status === "uploaded" && data.preview_complete;
  const confirmVisible = review || ready && manual;
  let title = "Выберите документы", description = "Анализ не запущен", counter = null, current = null;
  if (uploading) { title = "Загружаем документы"; description = "Передаём файлы и подготавливаем страницы"; current = "preview"; }
  else if (data && !data.preview_complete) { title = "Подготавливаем страницы"; current = "preview"; counter = { completed: data.pages_ready, total: data.pages_total }; }
  else if (ready) { title = "Документы готовы к анализу"; description = "Нажмите «Запустить анализ»"; }
  if (status === "queued" || starting) { title = "Ожидаем начала обработки"; description = "Задание поставлено в очередь"; counter = null; }
  if (status === "running" && active === "ocr") { title = "Распознаём текст документов"; counter = counters.ocr; current = "ocr"; description = "OCR выполняется"; }
  if (status === "running" && active === "boundaries") { title = "Определяем границы документов"; counter = data.comparisons; current = "boundaries"; description = "Сравниваем соседние страницы"; }
  if (status === "running" && active === "extraction") { title = "Извлекаем реквизиты"; counter = counters.extraction; current = "extraction"; description = "Анализируем утверждённые документы"; }
  if (status === "running" && active === "extraction" && currentDocument?.stage === "classification") title = "Определяем тип документа";
  if (review || ready && manual) { title = "Требуется ваша проверка"; description = `Предложено ${segmentation.document_count || 0} документов; ${data.comparisons?.errors || 0} границ требуют проверки`; current = "confirmation"; counter = null; }
  if (success) { title = "Обработка завершена"; description = "Результаты готовы"; current = "done"; counter = null; }
  if (success && documents.some(p => p.status !== "recognized")) {
    const supported = documents.filter(p => p.status === "recognized").length;
    const unsupported = documents.filter(p => p.status === "unsupported_template").length;
    const incomplete = documents.filter(p => ["classification_failed", "extraction_failed"].includes(p.status)).length;
    description = `Распознано писем: ${supported}. Без шаблона: ${unsupported}. Требуют проверки: ${incomplete}. PDF всех блоков готовы.`;
  }
  if (status === "failed" || error || unavailable) { title = "Не удалось завершить обработку"; description = data?.error?.message || error || "Задание недоступно. Создайте новый пакет."; counter = null; }
  if (cancelling) { title = "Отменяем обработку"; description = "Ожидаем подтверждения остановки"; counter = null; }
  else if (status === "cancelled") { title = "Обработка отменена"; description = "Можно начать новый пакет"; counter = null; }
  const validCounter = counter && Number.isInteger(counter.total) && counter.total > 0 && Number.isInteger(counter.completed) && counter.completed >= 0 && counter.completed <= counter.total;
  if (validCounter) {
    const prefix = current === "preview" ? "Подготовлено" : current === "ocr" ? "Распознано" : current === "boundaries" ? "Проверено" : "Обработано";
    description = `${prefix} ${counter.completed} из ${counter.total} ${current === "boundaries" ? "переходов" : current === "extraction" ? "документов" : "страниц"}`;
  }
  const progress = get("[data-progress]"); progress.hidden = !validCounter;
  if (validCounter) { progress.max = counter.total; progress.value = counter.completed; }
  else progress.removeAttribute("value");
  get("[data-processing-title]").textContent = title;
  get("[data-processing-description]").textContent = description;
  get("[data-processing-status]").textContent = cancelling ? "Остановка задания" : error ? "Ошибка" : statusNames[status] || "Анализ не запущен";
  get("[data-processing]").classList.toggle("processing--error", status === "failed" || !!error);
  const start = get("[data-start]"); start.hidden = !ready || confirmVisible || cancelling || unavailable;
  start.disabled = !ready || pending || starting || cancelling || unavailable;
  const confirm = get("[data-confirm-plan]"); confirm.hidden = !confirmVisible || cancelling || unavailable;
  confirm.disabled = !segmentation.can_confirm || pending || cancelling || unavailable;
  const cancel = get("[data-cancel]"); cancel.hidden = !data || finished.has(status) || review || ready || unavailable;
  cancel.disabled = cancelling;
  const download = get("[data-export]"); download.hidden = !success || !confirmed || unavailable; download.disabled = !confirmed || unavailable;
  get("[data-new-analysis]").disabled = false;
  const completed = {
    preview: !!data?.preview_complete,
    ocr: !!counters.ocr && counters.ocr.completed === counters.ocr.total,
    boundaries: !!segmentation.proposed_plan && !data?.comparisons?.errors,
    confirmation: !!segmentation.approved_plan,
    extraction: !!counters.extraction && counters.extraction.completed === counters.extraction.total,
    done: success,
  };
  for (const [step, name] of Object.entries(names)) {
    const node = get(`[data-processing-step="${step}"]`);
    const skipped = step === "boundaries" && mode.skip_segmentation || step === "confirmation" && data?.plan_locked && (mode.auto_split && !data.comparisons?.needs_review || mode.skip_segmentation && !manual);
    const failed = status === "failed" && step === active;
    const uncertain = step === "boundaries" && data?.comparisons?.needs_review;
    const state = failed ? "error" : skipped ? "skipped" : uncertain ? "review" : completed[step] ? "done" : step === current ? "active" : "pending";
    node.className = "processing__step" + (state === "pending" ? "" : ` processing__step--${state}`);
    node.textContent = `${state === "done" ? "✓ " : state === "error" ? "! " : ""}${name}${state === "skipped" ? " · пропущено" : uncertain ? " · требуется проверка" : ""}`;
    if (state === "active") node.setAttribute("aria-current", "step"); else node.removeAttribute("aria-current");
  }
}
