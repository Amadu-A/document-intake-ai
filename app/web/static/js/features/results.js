// app/web/static/js/features/results.js
/** Карточки VLM-плана, реквизиты и журнал без вставки недоверенного HTML. */
import { sourceLabel } from "./boundary_state.js";
export function renderPlan(snapshot, viewer) {
  /** Показывает регистрационные группы и приложения исключительно для просмотра. */
  const target = document.querySelector("[data-documents]"); target.replaceChildren();
  const segmentation = snapshot.segmentation;
  const documents = segmentation?.draft_plan?.documents || snapshot.plan?.documents || [];
  let count = 0;
  documents.slice(0, 60).forEach((doc, index) => {
    const card = document.createElement("article"); card.className = "structure__document";
    const title = document.createElement("h3"); title.textContent = `${index + 1}. ${doc.probable_kind || "Документ"} · ${doc.pages.length} стр.`;
    const pageButtons = document.createElement("div"); pageButtons.className = "structure__pages";
    doc.pages.slice(0, 6).forEach(ref => {
      const button = document.createElement("button"); button.type = "button"; button.className = "button button--compact";
      button.textContent = ref.page_number; button.setAttribute("aria-label", "Перейти к странице " + ref.page_number);
      button.addEventListener("click", () => viewer.select(ref)); pageButtons.append(button);
    });
    card.append(title, pageButtons);
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
  document.querySelector("[data-count-documents]").textContent = documents.length || "—";
  document.querySelector("[data-count-attachments]").textContent = documents.length ? count : "—";
  document.querySelector("[data-structure-status]").textContent = documents.length ?
    `${documents.length} регистрационных документов · ${count} приложений · ${segmentation?.locked ? "состав заблокирован" : "черновик состава"}${documents.length > 60 ? " · показаны первые 60, остальные доступны через ленту" : ""}` : "Здесь появится состав документов";
}

export function renderFacts(result) {
  /** Формирует доступное текстовое представление подтверждённых реквизитов и warnings. */
  const target = document.querySelector("[data-facts]"); target.replaceChildren();
  const warnings = document.querySelector("[data-warnings]"); warnings.replaceChildren();
  const allDocuments = result.files.flatMap(file => file.documents);
  document.querySelector("[data-count-attachments]").textContent = allDocuments.reduce((sum, doc) => sum + doc.requisites.attachments.length, 0);
  result.files.forEach(source => source.documents.forEach(doc => {
    const title = document.createElement("h3"); title.textContent = doc.requisites.title || "Документ"; target.append(title);
    const list = document.createElement("dl");
    for (const [label, value] of Object.entries({
      "Номер": doc.requisites.document_number, "Дата": doc.requisites.document_date,
      "Отправитель": doc.requisites.sender.name, "Адресат": doc.requisites.recipient.name,
      "Классификация": doc.classification.template_path.join(" → "),
      "Содержание": doc.requisites.summary,
    })) {
      const term = document.createElement("dt"), definition = document.createElement("dd");
      term.textContent = label; definition.textContent = value || "Не установлено"; list.append(term, definition);
    }
    const link = document.createElement("a"); link.href = doc.attachment_file.file_url; link.textContent = "↓ PDF-комплект для 1С";
    target.append(list, link);
    doc.warnings.forEach(text => { const item = document.createElement("li"); item.textContent = text; warnings.append(item); });
  }));
}

export function journal(event, data) {
  /** Хранит ограниченный визуальный журнал, не копируя в него приватный raw stream. */
  const item = document.createElement("li");
  item.textContent = (data.stage || data.status || data.code || event) + (data.attempt ? " · попытка " + data.attempt : "");
  const target = document.querySelector("[data-journal]"); target.append(item);
  while (target.children.length > 200) target.firstElementChild.remove();
}
