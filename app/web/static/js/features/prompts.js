// app/web/static/js/features/prompts.js
/** Две независимые сессионные настройки; закрытие сохраняет черновики до перезагрузки. */
import { api, notice } from "../api.js";
import { tabs } from "../components/tabs.js";

export function initializePrompts() {
  /** Подключает native dialog, вкладки, восстановление базового текста и атомарное сохранение обоих. */
  const dialog = document.querySelector("[data-prompts-dialog]");
  const editor = document.querySelector("[data-prompt-text]");
  const status = document.querySelector("[data-prompt-status]");
  let active = "segmentation", drafts = null, saved = null, versions = {}, bases = {}, returnFocus;
  function redraw() {
    /** Показывает активный текст и dirty-state, не перезаписывая другую вкладку. */
    if (!drafts) return;
    editor.value = drafts[active];
    document.querySelector("[data-prompt-label]").textContent = active === "segmentation" ?
      "Промпт 1 · анализ структуры пакета" : "Промпт 2 · классификация и реквизиты";
    document.querySelector("[data-prompt-version]").textContent = "Базовый " + (versions[active] || "v1");
    document.querySelector("#prompt-editor").setAttribute("aria-labelledby", "prompt-tab-" + active);
    updateStatus();
  }
  function updateStatus() {
    /** Показывает факт несохранённых правок и длину активного текста. */
    document.querySelector("[data-prompt-length]").textContent = drafts[active].length + " символов";
    status.textContent = JSON.stringify(drafts) === JSON.stringify(saved) ?
      "Сохранено для текущей сессии" : "Есть несохранённые изменения в черновике";
  }
  document.querySelector("[data-prompts-open]").addEventListener("click", async () => {
    try {
      if (!drafts) {
        const data = await api("/api/v1/session/prompts");
        drafts = { ...data.prompts }; saved = { ...data.prompts };
        versions = data.versions; bases = data.base_prompts;
      }
      returnFocus = document.activeElement;
      redraw(); dialog.showModal();
    } catch (error) { notice(error.message, true); }
  });
  document.querySelectorAll("[data-prompts-close]").forEach(button => button.addEventListener("click", () => dialog.close()));
  dialog.addEventListener("close", () => returnFocus?.focus());
  tabs(document.querySelectorAll("[data-prompt-tab]"), button => { active = button.dataset.promptTab; redraw(); });
  editor.addEventListener("input", () => { drafts[active] = editor.value; updateStatus(); });
  document.querySelector("[data-prompts-restore]").addEventListener("click", () => {
    drafts[active] = bases[active]; redraw();
  });
  document.querySelector("[data-prompts-save]").addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    try {
      const data = await api("/api/v1/session/prompts", { method: "PUT", body: JSON.stringify(drafts) });
      drafts = { ...data.prompts }; saved = { ...data.prompts }; redraw();
      status.textContent = "Оба промпта сохранены. Уже запущенные задания используют свои снимки.";
    } catch (error) { status.textContent = error.message; }
    finally { document.querySelector("[data-prompts-save]").disabled = false; }
  });
}
