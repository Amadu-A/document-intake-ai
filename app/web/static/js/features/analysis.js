// app/web/static/js/features/analysis.js
/** Загрузка, SSE/polling, reconnect, TTL и экспорт; сервер остаётся источником состояния. */
import { api, notice, downloadJSON } from "../api.js";
import { renderPlan, renderFacts, journal } from "./results.js";
import { createBoundaryEditor } from "./boundaries.js";

const terminal = new Set(["completed", "completed_with_warnings", "failed", "cancelled"]);
const labels = { uploaded: "Загружено", queued: "В очереди", running: "Обработка", awaiting_confirmation: "Ожидает проверки состава", completed: "Завершено", completed_with_warnings: "Завершено с предупреждениями", failed: "Ошибка", cancelled: "Отменено", expired: "Срок хранения истёк" };

export function initializeAnalysis(viewer) {
  /** Восстанавливает выбранное задание из sessionStorage без сохранения документов/ключей. */
  let jobId = sessionStorage.getItem("intake_job"), snapshot = null, confirmed = null, source = null, cursor = 0;
  let pollTimer = null, refreshTimer = null, raw = "", fetching = false, stopped = false;
  const start = document.querySelector("[data-start]");
  const input = document.querySelector("[data-files]");
  const json = document.querySelector("[data-json]");
  const boundaries = createBoundaryEditor(viewer, async () => {
    while (fetching) await new Promise(resolve => setTimeout(resolve, 30));
    await refresh();
  });
  async function state(data) {
    /** Применяет реальный snapshot, сохраняя ручной выбор страницы в viewer. */
    snapshot = data;
    document.querySelector("[data-job-status]").textContent = labels[data.status] || data.status;
    document.querySelector("[data-progress]").value = data.progress;
    start.disabled = data.status !== "uploaded" || (!data.preview_complete && data.stage !== "upload") || boundaries.pending();
    input.disabled = !terminal.has(data.status);
    document.querySelector("[data-cancel]").hidden = terminal.has(data.status);
    document.querySelector("[data-new-analysis]").hidden = !terminal.has(data.status);
    document.querySelector("[data-count-files]").textContent = data.sources.length;
    const queue = document.querySelector("[data-file-list]"); queue.replaceChildren();
    data.sources.forEach(file => { const item = document.createElement("li"); item.textContent = file.file_name; queue.append(item); });
    const enriched = await boundaries.update(data); renderPlan(enriched, viewer);
    if (!confirmed && data.status !== "running") {
      json.textContent = JSON.stringify({ status: data.status, plan_revision: enriched.segmentation.plan_revision, draft_plan: enriched.segmentation.draft_plan }, null, 2);
      document.querySelector("[data-validation]").textContent = "Черновик состава · реквизиты ещё не извлечены · экспорт недоступен";
    }
    const fallback = data.segmentation_source === "page_fallback";
    notice(data.error?.message || (fallback && data.status === "awaiting_confirmation" ? "Авторазделение не подтверждено. Предложен черновик по одному листу: объедините связанные страницы и подтвердите состав." : data.status === "awaiting_confirmation" ? "Проверьте состав документов и подтвердите извлечение" : labels[data.status]), data.status === "failed" || fallback && data.status === "awaiting_confirmation");
  }
  async function refresh() {
    /** Восстанавливает авторитетный snapshot и получает финальный JSON только после успеха. */
    if (!jobId || fetching || stopped) return;
    fetching = true;
    try {
      const data = await api(`/api/v1/jobs/${jobId}/snapshot?include_pages=false`); await state(data);
      if (["completed", "completed_with_warnings"].includes(data.status) && !confirmed) {
        confirmed = await api(`/api/v1/jobs/${jobId}/result`);
        json.textContent = JSON.stringify(confirmed, null, 2);
        document.querySelector("[data-validation]").textContent = "✓ Проверенный результат · schema_version 1.0";
        document.querySelector("[data-export]").disabled = false;
        renderFacts(confirmed);
        boundaries.result(confirmed);
      }
      if (terminal.has(data.status)) { source?.close(); clearTimeout(pollTimer); }
    } catch (error) {
      notice(error.message, true);
      if ([404, 410, 401].includes(error.status)) {
        stopped = true; boundaries.expire(); source?.close(); clearTimeout(pollTimer);
        document.querySelector("[data-export]").disabled = true;
        start.disabled = true;
        document.querySelector("[data-job-status]").textContent = labels.expired;
      }
    } finally { fetching = false; }
  }
  function connect() {
    /** EventSource повторяет соединение с cursor; polling закрывает разрывы и проверяет TTL. */
    source?.close(); source = new EventSource(`/api/v1/jobs/${jobId}/events?after=${cursor}&include_pages=false`);
    for (const kind of ["created", "queued", "started", "stage", "normalized", "source_discovered", "page_ready", "preview_ready", "mode_changed", "draft_saved", "plan_approved", "plan_locked", "segmentation", "segmentation_fallback", "ocr_retry", "model_retry", "retry", "warning", "terminal", "page_started", "text_delta", "expired"]) {
      source.addEventListener(kind, event => {
        cursor = Math.max(cursor, Number(event.lastEventId) || 0);
        const data = JSON.parse(event.data);
        if (kind === "text_delta") {
          if (!confirmed) {
            raw = (raw + data.text).slice(-200000); json.textContent = raw;
            document.querySelector("[data-validation]").textContent = "Поток VLM · данные ещё не проверены · экспорт недоступен";
          }
        } else if (kind !== "page_started") {
          journal(kind, data);
          if (!["model_retry", "retry", "warning", "ocr_retry"].includes(kind) && !refreshTimer) {
            refreshTimer = setTimeout(() => { refreshTimer = null; refresh(); }, 1000);
          }
        }
      });
    }
    source.onerror = () => { if (snapshot && terminal.has(snapshot.status)) source.close(); };
  }
  async function poll() {
    /** Переподключение не переключает страницу просмотра и не запускает анализ повторно. */
    await refresh();
    if (!stopped && snapshot && !terminal.has(snapshot.status)) pollTimer = setTimeout(poll, snapshot.preview_complete ? 3000 : 1200);
  }
  async function accept(files) {
    /** Сохраняет выбранную пачку без VLM-вызова; общий размер ограничивает сервер. */
    if (!files.length) return;
    clearTimeout(pollTimer); clearTimeout(refreshTimer); refreshTimer = null; source?.close(); confirmed = null; snapshot = null; cursor = 0; stopped = false; raw = "";
    start.disabled = true; document.querySelector("[data-export]").disabled = true;
    viewer.local(files[0]); notice("Загрузка оригиналов…");
    const form = new FormData(); [...files].forEach(file => form.append("files", file));
    for (const [key, value] of Object.entries(boundaries.flags())) form.append(key, String(value));
    try {
      const nonce = Array.from(crypto.getRandomValues(new Uint8Array(24)), byte => byte.toString(16).padStart(2, "0")).join("");
      const data = await api("/api/v1/uploads", { method: "POST", body: form, headers: { "Idempotency-Key": nonce } });
      jobId = data.job_id; sessionStorage.setItem("intake_job", jobId); await state(data); connect(); poll();
    } catch (error) { notice(error.message, true); input.disabled = false; }
  }
  input.addEventListener("change", () => accept(input.files));
  const dropzone = document.querySelector("[data-dropzone]");
  dropzone.addEventListener("dragover", event => { event.preventDefault(); dropzone.classList.add("upload--drag"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("upload--drag"));
  dropzone.addEventListener("drop", event => {
    event.preventDefault(); dropzone.classList.remove("upload--drag");
    if (!input.disabled) accept(event.dataTransfer.files);
  });
  start.addEventListener("click", async () => {
    start.disabled = true;
    try { await state(await api(`/api/v1/jobs/${jobId}/start`, { method: "POST" })); connect(); clearTimeout(pollTimer); poll(); }
    catch (error) { notice(error.message, true); start.disabled = false; }
  });
  document.querySelector("[data-cancel]").addEventListener("click", async () => {
    try { await state(await api(`/api/v1/jobs/${jobId}/cancel`, { method: "POST" })); source?.close(); }
    catch (error) { notice(error.message, true); }
  });
  document.querySelector("[data-new-analysis]").addEventListener("click", () => {
    sessionStorage.removeItem("intake_job"); location.reload();
  });
  document.querySelector("[data-export]").addEventListener("click", () => { if (confirmed) downloadJSON(confirmed); });
  setInterval(() => {
    if (!snapshot?.expires_at) return;
    const seconds = Math.max(0, Math.ceil((Date.parse(snapshot.expires_at) - Date.now()) / 1000));
    document.querySelector("[data-ttl]").textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} до удаления`;
    if (!seconds && !stopped) { stopped = true; boundaries.expire(); source?.close(); start.disabled = true; document.querySelector("[data-export]").disabled = true; notice("Срок хранения истёк. Создайте новый пакет.", true); }
  }, 1000);
  if (jobId) { refresh().then(() => { if (!stopped && snapshot && !terminal.has(snapshot.status)) { connect(); poll(); } }); }
}
