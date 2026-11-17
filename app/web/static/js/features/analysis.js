// app/web/static/js/features/analysis.js
/** Загрузка, отмена и смена пакета с защитой от запоздавших ответов. */
import { api, notice, downloadJSON } from "../api.js";
import { clearPreviews } from "../previews.js";
import { renderPlan, renderFacts, journal } from "./results.js";
import { createBoundaryEditor } from "./boundaries.js";
import { renderProcessing } from "./processing.js";

const terminal = new Set(["completed", "completed_with_warnings", "failed", "cancelled"]);
const labels = { uploaded: "Загружено", queued: "В очереди", running: "Обработка", awaiting_confirmation: "Ожидает проверки состава", completed: "Завершено", completed_with_warnings: "Завершено с предупреждениями", failed: "Ошибка", cancelled: "Отменено" };

export function initializeAnalysis(viewer) {
  /** Поколение пакета защищает все асинхронные продолжения и reconnect. */
  let jobId = sessionStorage.getItem("intake_job"), snapshot = null, confirmed = null;
  let source = null, cursor = 0, pollTimer = null, refreshTimer = null, raw = "";
  let generation = 0, fetching = null, stopped = false, uploading = false, cancelling = false, exported = false;
  let starting = false, transportError = null;
  let reads = new AbortController();
  const get = selector => document.querySelector(selector);
  const start = get("[data-start]"), input = get("[data-files]"), organization = get("[data-organization]"), json = get("[data-json]");
  const boundaries = createBoundaryEditor(viewer, async () => {
    const own = generation;
    while (fetching === own) await new Promise(resolve => setTimeout(resolve, 30));
    if (own === generation) await refresh();
  }, paint);
  function paint() {
    /** Перерисовывает панель из того же snapshot, что используют просмотр и редактор. */
    renderProcessing(snapshot, { uploading, cancelling, starting, pending: boundaries.pending(), confirmed: !!confirmed, error: transportError, unavailable: stopped });
  }
  const current = (own, id) => own === generation && id === jobId;
  function close() {
    /** Отзывает чтения, SSE, таймеры и декодирование превью старого поколения. */
    source?.close(); source = null; clearTimeout(pollTimer); clearTimeout(refreshTimer);
    pollTimer = refreshTimer = null; reads.abort(); reads = new AbortController(); clearPreviews();
  }
  function reset() {
    /** Полностью освобождает пакет без перезагрузки страницы. */
    generation++; close(); jobId = null; snapshot = confirmed = null; cursor = 0; raw = "";
    fetching = null; stopped = uploading = cancelling = exported = starting = false; transportError = null;
    sessionStorage.removeItem("intake_job"); input.value = ""; input.disabled = false;
    organization.value = ""; organization.disabled = false; start.disabled = true;
    get("[data-cancel]").hidden = true; get("[data-cancel]").disabled = false;
    get("[data-export]").disabled = true;
    get("[data-job-status]").textContent = "Ожидание"; get("[data-ttl]").textContent = "";
    get("[data-count-files]").textContent = "0";
    for (const name of ["documents", "attachments"]) get(`[data-count-${name}]`).textContent = "—";
    for (const name of ["file-list", "documents", "facts", "warnings", "journal"]) get(`[data-${name}]`).replaceChildren();
    json.textContent = "Ожидание анализа…"; get("[data-validation]").textContent = "Реквизиты ещё не извлечены";
    get("[data-structure-status]").textContent = "Здесь появится состав документов";
    boundaries.reset(); viewer.reset(); paint(); notice("Выберите файлы нового пакета");
  }
  async function state(data, own = generation) {
    /** Проверка поколения повторяется после каждого сетевого ожидания. */
    if (!current(own, data.job_id)) return false;
    const previousStatus = snapshot?.status, previousPairs = snapshot?.comparisons?.completed, previousLocked = snapshot?.plan_locked;
    snapshot = data; if (terminal.has(data.status)) cancelling = false;
    get("[data-job-status]").textContent = cancelling ? "Отмена…" : labels[data.status] || data.status;
    input.disabled = !terminal.has(data.status); organization.disabled = input.disabled;
    organization.value = data.organization_hint || "";
    get("[data-count-files]").textContent = data.sources.length;
    get("[data-file-list]").replaceChildren(...data.sources.map(file => { const item = document.createElement("li"); item.textContent = file.file_name; return item; }));
    const enriched = await boundaries.update(data);
    if (!enriched || !current(own, data.job_id)) return false;
    snapshot = enriched; paint();
    if (previousStatus !== data.status) journal("status", data);
    if (previousPairs !== data.comparisons?.completed && data.comparisons?.total) journal("comparisons_progress", data.comparisons);
    if (enriched.segmentation.locked && !previousLocked) journal("approved", { count: enriched.segmentation.document_count });
    renderPlan(enriched, viewer);
    if (!confirmed && data.status !== "running") {
      json.textContent = JSON.stringify({ status: data.status, plan_revision: enriched.segmentation.plan_revision, draft_plan: enriched.segmentation.draft_plan }, null, 2);
      get("[data-validation]").textContent = "Черновик состава · реквизиты ещё не извлечены · экспорт недоступен";
    }
    const partial = data.comparisons?.needs_review;
    notice(data.error?.message || (partial ? "Есть неопределённые переходы. Проверьте отмеченные страницы и подтвердите состав." : data.status === "awaiting_confirmation" ? "Проверьте состав документов и подтвердите извлечение" : labels[data.status]), data.status === "failed" || partial);
    return true;
  }
  async function refresh() {
    /** Финальный JSON запрашивается только после успешного статуса. */
    if (!jobId || fetching === generation || stopped) return;
    const own = generation, id = jobId, signal = reads.signal; fetching = own;
    try {
      const data = await api(`/api/v1/jobs/${id}/snapshot?include_pages=false`, { signal });
      if (!current(own, id) || !await state(data, own)) return;
      if (["completed", "completed_with_warnings"].includes(data.status) && !confirmed) {
        const result = await api(`/api/v1/jobs/${id}/result`, { signal });
        if (!current(own, id)) return;
        confirmed = result; json.textContent = JSON.stringify(result, null, 2);
        get("[data-validation]").textContent = "✓ Проверенный результат · schema_version 1.0";
        get("[data-export]").disabled = false; renderFacts(result); boundaries.result(result);
        journal("json_ready", {}); paint();
      }
      if (terminal.has(data.status)) { source?.close(); clearTimeout(pollTimer); }
    } catch (error) {
      if (!current(own, id) || error.name === "AbortError") return;
      notice(error.message, true);
      if ([404, 410, 401].includes(error.status)) {
        stopped = true; boundaries.expire(); close(); start.disabled = true;
        get("[data-export]").disabled = true; get("[data-job-status]").textContent = "Срок хранения истёк";
        paint();
      }
    } finally { if (fetching === own) fetching = null; }
  }
  function connect() {
    /** Все обработчики замыкают идентификатор и поколение своего задания. */
    source?.close(); const own = generation, id = jobId;
    const stream = new EventSource(`/api/v1/jobs/${id}/events?after=${cursor}&include_pages=false`); source = stream;
    for (const kind of ["created", "queued", "started", "stage", "normalized", "source_discovered", "page_ready", "preview_ready", "mode_changed", "draft_saved", "plan_approved", "plan_locked", "segmentation", "pair_started", "pair_result", "ocr_progress", "extraction_progress", "ocr_retry", "model_retry", "retry", "warning", "terminal", "text_delta", "expired"]) {
      stream.addEventListener(kind, event => {
        if (!current(own, id) || cancelling) return;
        cursor = Math.max(cursor, Number(event.lastEventId) || 0); const data = JSON.parse(event.data);
        if (kind === "text_delta") {
          if (!confirmed) { raw = (raw + data.text).slice(-200000); json.textContent = raw; get("[data-validation]").textContent = "Поток VLM · данные ещё не проверены · экспорт недоступен"; }
        } else {
          journal(kind, data);
          if (!refreshTimer) refreshTimer = setTimeout(() => { if (current(own, id)) { refreshTimer = null; refresh(); } }, 500);
        }
      });
    }
    stream.onerror = () => { if (!current(own, id) || snapshot && terminal.has(snapshot.status)) stream.close(); };
  }
  async function poll(own = generation, id = jobId) {
    /** Старый таймер никогда не запускает polling нового пакета. */
    if (!current(own, id)) return; await refresh();
    if (current(own, id) && !stopped && snapshot && !terminal.has(snapshot.status)) pollTimer = setTimeout(() => poll(own, id), snapshot.preview_complete ? 3000 : 1200);
  }
  async function cancelJob(id) {
    /** Конечное ожидание отмены оставляет доступными повтор и явное отсоединение. */
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 10000);
    try { return await api(`/api/v1/jobs/${id}/cancel`, { method: "POST", signal: controller.signal }); }
    finally { clearTimeout(timer); }
  }
  async function newPacket() {
    /** Отмена активного пакета предшествует сбросу; ошибка не выдаётся за успех. */
    if (cancelling) {
      if (!window.confirm("Отмена ещё не подтверждена. Отсоединиться и создать новый пакет? Старый пакет может продолжить обработку.")) return false;
      reset(); return true;
    }
    if (uploading && !window.confirm("Загрузка ещё выполняется. Отсоединиться? Сервер может продолжить её; после ответа будет запрошена отмена старого задания.")) return false;
    if (jobId && !terminal.has(snapshot?.status)) {
      if (!window.confirm("Отменить текущий пакет и начать новый?")) return false;
      cancelling = true; get("[data-job-status]").textContent = "Отмена…"; paint();
      try { await cancelJob(jobId); }
      catch { if (!window.confirm("Сервер не подтвердил отмену. Отсоединиться и создать новый пакет? Старый пакет может продолжить обработку.")) { cancelling = false; paint(); poll(); return false; } }
    } else if (confirmed && !exported && !window.confirm("Результат ещё не экспортирован. Начать новый пакет?")) return false;
    reset(); return true;
  }
  async function accept(selected) {
    /** Поздний ответ загрузки отменяет только старое задание, не меняя новый экран. */
    const files = Array.from(selected); input.value = ""; if (!files.length) return;
    if ((jobId || uploading) && !await newPacket()) return;
    const own = generation; uploading = true; transportError = null; input.disabled = true; start.disabled = true; paint();
    get("[data-export]").disabled = true; viewer.local(files[0]); notice("Загрузка оригиналов…");
    const form = new FormData(); files.forEach(file => form.append("files", file));
    if (organization.value.trim()) form.append("organization_hint", organization.value.trim());
    organization.disabled = true; for (const [key, value] of Object.entries(boundaries.flags())) form.append(key, String(value));
    try {
      const nonce = Array.from(crypto.getRandomValues(new Uint8Array(24)), byte => byte.toString(16).padStart(2, "0")).join("");
      const data = await api("/api/v1/uploads", { method: "POST", body: form, headers: { "Idempotency-Key": nonce } });
      if (own !== generation) { cancelJob(data.job_id).catch(() => {}); return; }
      uploading = false; jobId = data.job_id; sessionStorage.setItem("intake_job", jobId);
      await state(data, own); if (!current(own, data.job_id)) return; connect(); poll();
    } catch (error) { if (own === generation) { uploading = false; transportError = error.message; input.disabled = false; organization.disabled = false; paint(); notice(error.message, true); } }
  }
  input.addEventListener("change", () => accept(input.files));
  const dropzone = get("[data-dropzone]");
  dropzone.addEventListener("dragover", event => { event.preventDefault(); dropzone.classList.add("upload--drag"); });
  dropzone.addEventListener("dragleave", () => dropzone.classList.remove("upload--drag"));
  dropzone.addEventListener("drop", event => { event.preventDefault(); dropzone.classList.remove("upload--drag"); if (!input.disabled) accept(event.dataTransfer.files); });
  start.addEventListener("click", async () => {
    const own = generation, id = jobId; starting = true; paint();
    try { const data = await api(`/api/v1/jobs/${id}/start`, { method: "POST" }); if (current(own, id)) { await state(data, own); if (current(own, id)) { connect(); clearTimeout(pollTimer); poll(); } } }
    catch (error) { if (current(own, id)) notice(error.message, true); }
    finally { if (current(own, id)) { starting = false; paint(); } }
  });
  get("[data-cancel]").addEventListener("click", async () => {
    if (cancelling || !jobId) return; const own = generation, id = jobId;
    cancelling = true; get("[data-job-status]").textContent = "Отмена…"; paint();
    try { const data = await cancelJob(id); if (current(own, id)) { cancelling = false; await state(data, own); source?.close(); } }
    catch (error) { if (current(own, id)) { cancelling = false; paint(); notice("Отмена не подтверждена. Повторите отмену или выберите «Новый пакет».", true); } }
  });
  get("[data-new-analysis]").hidden = false; get("[data-new-analysis]").addEventListener("click", newPacket);
  get("[data-export]").addEventListener("click", () => { if (confirmed) { downloadJSON(confirmed); exported = true; } });
  setInterval(() => {
    if (!snapshot?.expires_at) return;
    const seconds = Math.max(0, Math.ceil((Date.parse(snapshot.expires_at) - Date.now()) / 1000));
    get("[data-ttl]").textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")} до удаления`;
    if (!seconds && !stopped) { stopped = true; close(); boundaries.expire(); paint(); notice("Срок хранения истёк. Создайте новый пакет.", true); }
  }, 1000);
  if (jobId) { const own = generation; refresh().then(() => { if (own === generation && !stopped && snapshot && !terminal.has(snapshot.status)) { connect(); poll(); } }); }
  paint();
}
