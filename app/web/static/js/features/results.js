// app/web/static/js/features/results.js
/** Карточки VLM-плана, реквизиты и журнал без вставки недоверенного HTML. */
import { sourceLabel } from "./boundary_state.js";
const processingLabels = { recognized: "Реквизиты извлечены", unsupported_template: "Ранний результат без шаблона", classification_failed: "Тип не подтверждён · PDF сохранён", extraction_failed: "Реквизиты не подтверждены · PDF сохранён" };
export function renderPlan(snapshot, viewer) {
  /** Показывает регистрационные группы и приложения исключительно для просмотра. */
  const target = document.querySelector("[data-documents]"); target.replaceChildren();
  const segmentation = snapshot.segmentation;
  const documents = segmentation?.draft_plan?.documents || snapshot.plan?.documents || [];
  let count = 0;
  documents.slice(0, 60).forEach((doc, index) => {
    const card = document.createElement("article"); card.className = "structure__document";
    const initial = segmentation?.segmentation_source === "source_files" && !segmentation.locked;
    const title = document.createElement("h3"); title.textContent = `${index + 1}. ${doc.probable_kind || (initial ? "Исходный файл" : "Документ")} · ${doc.pages.length} стр.`;
    const pageButtons = document.createElement("div"); pageButtons.className = "structure__pages";
    doc.pages.slice(0, 6).forEach(ref => {
      const button = document.createElement("button"); button.type = "button"; button.className = "button button--compact";
      button.textContent = ref.page_number; button.setAttribute("aria-label", "Перейти к странице " + ref.page_number);
      button.addEventListener("click", () => viewer.select(ref)); pageButtons.append(button);
    });
    card.append(title, pageButtons);
    const processing = Object.values(snapshot.document_processing || {}).find(p => p.segment_index === index + 1);
    if (processingLabels[processing?.status]) {
      const status = document.createElement("p"); status.className = "structure__reason";
      status.textContent = processingLabels[processing.status]; card.append(status);
    }
    const proposal = snapshot.plan?.documents?.[index] || segmentation?.proposed_plan?.documents?.[index];
    const attachments = segmentation?.segmentation_source?.startsWith("manual") ? [] : (doc.attachments || proposal?.attachments || []);
    attachments.forEach(attachment => {
      count += 1;
      const item = document.createElement("div"); item.className = "structure__attachment";
      const button = document.createElement("button"); button.type = "button"; button.className = "button button--compact button--page-attachment";
      button.textContent = "✓ " + attachment.kind + " · стр. " + attachment.pages.map(p => p.page_number).join(", ");
      button.addEventListener("click", () => viewer.select(attachment.pages[0])); item.append(button); card.append(item);
    });
    const reason = document.createElement("p"); reason.className = "structure__reason"; reason.textContent = doc.reason || sourceLabel(segmentation?.segmentation_source); card.append(reason);
    target.append(card);
  });
  const initial = segmentation?.segmentation_source === "source_files" && !segmentation.locked;
  document.querySelector("[data-count-documents]").textContent = !initial && documents.length || "—";
  document.querySelector("[data-count-attachments]").textContent = documents.length ? count : "—";
  document.querySelector("[data-structure-status]").textContent = initial && !segmentation.skip_segmentation ? `Загружен${snapshot.sources.length === 1 ? " 1 файл" : "о файлов: " + snapshot.sources.length} · ${snapshot.pages_total} страниц. Границы ещё не определены` : documents.length ?
    `${documents.length} ${segmentation?.locked ? "регистрационных документов" : "диапазонов черновика"} · ${count} приложений · ${segmentation?.locked ? "состав заблокирован" : "черновик состава"}${documents.length > 60 ? " · показаны первые 60, остальные доступны через ленту" : ""}` : "Здесь появится состав документов";
}

export function renderFacts(result, processing = {}, detailed = null, viewer = null) {
  /** Формирует доступное текстовое представление подтверждённых реквизитов и warnings. */
  const target = document.querySelector("[data-facts]"); target.replaceChildren();
  const warnings = document.querySelector("[data-warnings]"); warnings.replaceChildren();
  const allDocuments = result.files.flatMap(file => file.documents);
  document.querySelector("[data-count-attachments]").textContent = allDocuments.reduce((sum, doc) => sum + doc.requisites.attachments.length, 0);
  result.files.forEach(source => source.documents.forEach(doc => {
    const title = document.createElement("h3"); title.textContent = doc.requisites.title || "Документ"; target.append(title);
    if (processingLabels[processing[doc.document_id]?.status]) {
      const status = document.createElement("p"); status.className = "result__validation";
      status.textContent = processingLabels[processing[doc.document_id].status]; target.append(status);
    }
    const list = document.createElement("dl");
    for (const [label, value] of Object.entries({
      "Номер": doc.requisites.document_number, "Дата": doc.requisites.document_date,
      "Отправитель": doc.requisites.sender.name, "Адресат": doc.requisites.recipient.name,
      "Фактический вид": doc.classification.document_kind,
      "Классификация": (doc.classification.template_path.length ? doc.classification.template_path : [doc.classification.document_root, doc.classification.document_kind, doc.classification.document_subkind].filter(Boolean)).join(" → "),
      "Содержание": doc.requisites.summary,
    })) {
      const term = document.createElement("dt"), definition = document.createElement("dd");
      term.textContent = label; definition.textContent = value || "Не установлено"; list.append(term, definition);
    }
    const link = document.createElement("a"); link.href = doc.attachment_file.file_url; link.textContent = "↓ PDF-комплект для 1С";
    target.append(list, link);
    const extended = detailed?.documents.find(item => item.document_id === doc.document_id);
    if (extended) {
      const category = document.createElement("p"); category.textContent = extended.kind_name ? "Категория: " + extended.kind_name : "Категория не установлена · общие реквизиты"; target.append(category);
      const labels = {primary_document_type:"Фактический вид",primary_document_role:"Назначение",parent_contract:"Договор-основание",total_amount:"Общая сумма",vat_amount:"НДС",currency:"Валюта",payment_purpose:"Назначение платежа",payment_terms:"Условия оплаты",bank_details:"Банковские реквизиты",court_name:"Суд",court_case_number:"Номер дела",issue_date:"Дата выдачи",claimant:"Взыскатель",debtor:"Должник",enforcement_subject:"Предмет взыскания",contract_subject:"Предмет договора",our_role:"Наша роль",valid_from:"Начало действия",valid_until:"Окончание действия",service_subject:"Предмет обслуживания",change_subject:"Предмет изменения",effective_date:"Дата вступления",disputed_terms:"Разногласия",attachment_subject:"Содержание приложения"};
      const roles = {primary:"Подтверждение исполнения",tax:"Налоговый документ",payment_request:"Запрос платежа",reconciliation:"Сверка"};
      const fields = document.createElement("dl");
      for (const [key, value] of Object.entries(extended.details || {})) {
        if (key === "profile") continue;
        const term = document.createElement("dt"), definition = document.createElement("dd"); term.textContent = labels[key] || "Реквизит";
        definition.textContent = value == null ? "Не установлено" : key === "primary_document_role" ? roles[value] || value : typeof value === "object" ? Object.values(value).filter(v => v != null).join(" · ") || "Не установлено" : String(value); fields.append(term, definition);
      }
      target.append(fields);
      for (const [path, records] of Object.entries(extended.field_evidence)) for (const record of records) {
        if (!viewer) continue;
        const evidence = document.createElement("button"); evidence.type = "button"; evidence.className = "button button--compact";
        const field = path.split('.').at(-1), label = labels[field] || ({document_number:"Номер",document_date:"Дата",document_kind:"Вид",name:"Организация",summary:"Содержание"})[field] || "Подтверждение";
        evidence.textContent = `${label} · исходная стр. ${record.page_number}`; evidence.title = record.raw || "Свидетельство чтения";
        evidence.addEventListener("click", () => viewer.select({ file_id: record.file_id, page_number: record.page_number })); target.append(evidence);
      }
    }
    doc.warnings.forEach(text => { const item = document.createElement("li"); item.textContent = text; warnings.append(item); });
  }));
}

export function journal(event, data) {
  /** Хранит ограниченный визуальный журнал, не копируя в него приватный raw stream. */
  const stageNames = { preparing_preview: "Подготавливаем страницы", docling: "Распознаём текст документов", segmentation_vlm: "Определяем границы документов", extraction_vlm: "Извлекаем реквизиты" };
  const statuses = { awaiting_confirmation: "Ожидается подтверждение пользователя", completed: "Обработка завершена", completed_with_warnings: "Обработка завершена с предупреждениями", failed: "Обработка завершилась ошибкой", cancelled: "Обработка отменена" };
  const messages = { created: "Файлы загружены", queued: "Задание поставлено в очередь", started: "Началась обработка", preview_ready: "Превью готовы", segmentation: "Определение границ завершено", plan_approved: "Диапазоны подтверждены", draft_saved: "Разметка сохранена", mode_changed: "Режим изменён", ocr_retry: "OCR ожидает освобождения сервиса", model_retry: "Повторная проверка ответа модели", retry: "Обработка будет повторена", warning: "Обнаружено предупреждение", expired: "Срок хранения истёк" };
  let text = event === "stage" ? stageNames[data.stage] : event === "terminal" || event === "status" ? statuses[data.status] : messages[event];
  if (event === "ocr_progress") text = `OCR: обработано ${data.completed} из ${data.total} страниц`;
  if (event === "extraction_progress") text = `Извлечение реквизитов: ${data.completed} из ${data.total}`;
  if (event === "comparisons_progress") text = `Проверка границ: ${data.completed} из ${data.total}`;
  if (event === "approved") text = `Подтверждены ${data.count} документов`;
  if (event === "json_ready") text = "JSON сформирован";
  if (["document_processing", "document_completed"].includes(event) && data.processing) {
    const p = data.processing;
    text = `Документ ${p.segment_index}: ` + (processingLabels[p.status] || (p.stage === "classification" ? "Определяем тип" : "Извлекаем реквизиты"));
  }
  if (event === "pair_result" && data.status === "unresolved") text = "Требуется проверка границы между страницами";
  if (!text) return;
  if (document.querySelector("[data-journal]").lastElementChild?.textContent === text) return;
  const item = document.createElement("li"); item.textContent = text;
  const target = document.querySelector("[data-journal]"); target.append(item);
  while (target.children.length > 200) target.firstElementChild.remove();
}
