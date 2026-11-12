// app/web/static/js/features/boundaries.js
/** Редактор с серверным ACK/revision, ограниченным DOM и ленивой загрузкой превью. */
import { api, notice } from "../api.js";
import { previewURL, clearPreviews } from "../previews.js";
import { pageKey, sourceLabel, groupFor, splitRange, mergeRanges, excludePages, createRange, moveBoundary, coversPages } from "./boundary_state.js";

export function createBoundaryEditor(viewer, changed) {
  /** Хранит метаданные; изображения и URL запрашивает порциями по 60 страниц. */
  let snapshot = null, saved = null, savedStatus = null, plan = null, physical = [], filtered = [], selected = new Set(), anchor = 0;
  let busy = false, history = [], generation = 0, editing = null, lastJob = null;
  let activePageKey = null;
  const cache = new Map(), requests = new Map();
  const finalRoles = new Map();
  const scroll = document.querySelector("[data-boundary-scroll]"), track = document.querySelector("[data-boundary-track]");
  const auto = document.querySelector("[data-auto-split]"), skip = document.querySelector("[data-skip-segmentation]");
  const saveLabel = document.querySelector("[data-boundary-save]"), confirm = document.querySelector("[data-confirm-plan]");
  const sourceSelect = document.querySelector("[data-source-select]");
  const errorLabel = document.querySelector("[data-boundary-error]");
  const endpoint = () => `/api/v1/jobs/${snapshot.job_id}`;
  function flags() { /** Возвращает значения переключателей для multipart. */ return { auto_split: auto.checked, skip_segmentation: skip.checked }; }
  async function metadata(ref) {
    /** Один запрос на окно; signed URL обновляется до истечения пяти минут. */
    if (!snapshot) return null;
    const key = pageKey(ref), stored = cache.get(key);
    if (stored && Date.now() - stored.time < (stored.page.render_status === "ready" ? 60000 : 1200)) return stored.page;
    const position = physical.findIndex(p => pageKey(p) === key), offset = Math.floor(position / 60) * 60;
    if (position < 0) return null;
    const requestKey = `${generation}:${offset}`, ownGeneration = generation;
    if (!requests.has(requestKey)) requests.set(requestKey, api(`${endpoint()}/pages?offset=${offset}&limit=60`).then(data => {
      if (ownGeneration === generation) for (const page of data.pages) cache.set(pageKey(page), { page, time: Date.now() });
    }).finally(() => requests.delete(requestKey)));
    await requests.get(requestKey); return cache.get(key)?.page;
  }
  function description(ref) {
    /** Различает ручной диапазон, предложение VLM и исключённую страницу. */
    const group = groupFor(plan, ref), index = plan?.documents.indexOf(group);
    if (finalRoles.has(pageKey(ref))) return finalRoles.get(pageKey(ref));
    const proposed = saved?.segmentation_source?.startsWith("vlm") ? saved.proposed_plan?.documents : [];
    for (const doc of proposed || []) for (const attachment of doc.attachments) if (attachment.pages.some(p => pageKey(p) === pageKey(ref))) return `Приложение: ${attachment.kind} · документ ${doc.document_index}`;
    return group ? `Документ ${index + 1} · ${sourceLabel(saved?.segmentation_source)}` : "Вне диапазона · оригинал сохранён";
  }
  function selection(ref, extend = false, toggle = false) {
    /** Shift выбирает интервал, Ctrl/Cmd — отдельные несмежные листы без захвата чужих. */
    const position = physical.findIndex(p => pageKey(p) === pageKey(ref));
    activePageKey = pageKey(ref);
    if (toggle) { const key = pageKey(ref); if (selected.has(key)) selected.delete(key); else selected.add(key); anchor = position; }
    else if (!extend) { selected = new Set([pageKey(ref)]); anchor = position; }
    else for (const p of physical.slice(Math.min(anchor, position), Math.max(anchor, position) + 1)) selected.add(pageKey(p));
    document.querySelector("[data-page-jump]").value = position + 1;
    document.querySelector("[data-boundary-selection]").textContent = `Выбрано страниц: ${selected.size}`; render();
  }
  viewer.configure(metadata, ref => { if (activePageKey !== pageKey(ref) && !selected.has(pageKey(ref))) selection(ref); activePageKey = pageKey(ref); }, description);
  function controls() {
    /** Подтверждение доступно для полностью сохранённого и покрытого плана. */
    const locked = !snapshot || snapshot.plan_locked || ["failed", "cancelled", "completed", "completed_with_warnings", "expired"].includes(snapshot.status) || (snapshot.expires_at && Date.parse(snapshot.expires_at) <= Date.now());
    skip.disabled = busy || locked && !!snapshot;
    auto.disabled = skip.disabled || skip.checked;
    for (const button of document.querySelectorAll("[data-boundary-action]")) button.disabled = busy || locked || !physical.length || (button.dataset.boundaryAction === "undo" && !history.length);
    confirm.hidden = !saved || saved.locked || !(saved.skip_segmentation || snapshot?.status === "awaiting_confirmation");
    confirm.disabled = locked || busy || !!editing || !saved?.can_confirm || !coversPages(plan, physical) || !plan.documents.length;
    document.querySelector("[data-mode-hint]").textContent = auto.checked ? "Автоматическое разделение и извлечение без подтверждения" : skip.checked ? "Границы VLM пропущены · исходные файлы или ручные диапазоны" : "VLM предложит состав · извлечение после подтверждения";
  }
  function render() {
    /** Видимые карточки с overscan: размер DOM ограничен независимо от числа страниц. */
    filtered = sourceSelect.value ? physical.filter(p => p.file_id === sourceSelect.value) : physical;
    const scrollPosition = scroll.scrollLeft;
    const first = Math.max(0, Math.floor(scroll.scrollLeft / 108) - 3), count = Math.min(24, Math.ceil(scroll.clientWidth / 108) + 6);
    const focused = track.contains(document.activeElement) ? document.activeElement.getAttribute("aria-label") : null;
    const fragment = document.createDocumentFragment();
    const spacer = width => { const node = document.createElement("div"); node.className = "boundary__spacer"; node.style.width = Math.max(0, width) + "px"; fragment.append(node); };
    if (first) spacer(first * 108 - 8);
    const localGeneration = generation;
    for (const ref of filtered.slice(first, first + count)) {
      const group = groupFor(plan, ref), groupIndex = plan?.documents.indexOf(group), global = physical.findIndex(p => pageKey(p) === pageKey(ref));
      const card = document.createElement("div"); card.dataset.position = global;
      card.className = "boundary__page" + (group ? ` boundary__page--tone-${groupIndex % 6}` : " boundary__page--excluded") + (selected.has(pageKey(ref)) ? " boundary__page--selected" : "");
      const button = document.createElement("button"); button.type = "button"; button.className = "boundary__select";
      button.setAttribute("aria-label", `Просмотреть страницу ${global + 1}`); button.setAttribute("aria-pressed", String(selected.has(pageKey(ref))));
      const pending = document.createElement("span"); pending.className = "boundary__pending"; pending.textContent = "Готовим превью…";
      const number = document.createElement("span"); number.className = "boundary__number"; number.textContent = `Стр. ${global + 1} · исходная ${ref.page_number}`;
      const title = document.createElement("span"); title.className = "boundary__role"; title.textContent = group ? `Документ ${groupIndex + 1}` : "Вне диапазона";
      button.append(pending, number, title); button.addEventListener("click", e => { selection(ref, e.shiftKey, e.ctrlKey || e.metaKey); viewer.select(ref); }); card.append(button);
      metadata(ref).then(async meta => {
        if (generation !== localGeneration || !card.isConnected || !meta?.thumbnail_url) return;
        const url = await previewURL(meta.thumbnail_url);
        if (generation !== localGeneration || !card.isConnected) return;
        const image = document.createElement("img"); image.className = "boundary__image"; image.src = url; image.alt = ""; image.loading = "lazy"; pending.replaceWith(image);
      }).catch(() => {});
      if (group && !saved?.locked) {
        if (pageKey(group.pages[0]) === pageKey(ref)) handle(card, group, "left", global);
        if (pageKey(group.pages.at(-1)) === pageKey(ref)) handle(card, group, "right", global);
      }
      fragment.append(card);
    }
    const tail = Math.max(0, filtered.length - first - count); if (tail) spacer(tail * 108 - 8);
    track.replaceChildren(fragment);
    scroll.scrollLeft = scrollPosition;
    if (focused) Array.from(track.querySelectorAll("button[aria-label]")).find(button => button.getAttribute("aria-label") === focused)?.focus({ preventScroll: true });
    controls();
  }
  function handle(card, group, side, position) {
    /** Мышь и клавиатура используют общую проверяемую операцию перемещения. */
    const button = document.createElement("button"); button.type = "button"; button.className = "boundary__handle" + (side === "left" ? " boundary__handle--left" : "");
    button.textContent = "↔"; button.disabled = busy; button.setAttribute("aria-label", `${side === "left" ? "Начало" : "Конец"} документа ${plan.documents.indexOf(group) + 1}`);
    const original = () => { if (!editing) editing = { plan: structuredClone(plan), id: group.client_id, side, target: position }; };
    const move = target => {
      original();
      try { plan = moveBoundary(editing.plan, editing.id, side, target, physical); editing.target = target; saveLabel.textContent = "Граница изменена локально · Enter — сохранить, Escape — отменить"; saveLabel.classList.add("boundary__save--pending"); render(); }
      catch (error) { notice(error.message, true); }
    };
    button.addEventListener("keydown", e => {
      if (["ArrowLeft", "ArrowRight"].includes(e.key)) {
        e.preventDefault(); original(); move(editing.target + (e.key === "ArrowLeft" ? -1 : 1)); track.querySelector(`[aria-label="${button.getAttribute("aria-label")}"]`)?.focus();
      } else if (e.key === "Enter" && editing) { e.preventDefault(); save(plan, "boundary_move"); }
      else if (e.key === "Escape" && editing) { plan = editing.plan; editing = null; render(); savedMessage(); }
    });
    button.addEventListener("pointerdown", e => {
      if (busy || e.button !== 0) return;
      e.preventDefault(); original(); const origin = e.clientX, startScroll = scroll.scrollLeft;
      const drag = event => {
        if (event.clientX > scroll.getBoundingClientRect().right - 30) scroll.scrollLeft += 35;
        if (event.clientX < scroll.getBoundingClientRect().left + 30) scroll.scrollLeft -= 35;
        const delta = Math.round((event.clientX - origin + scroll.scrollLeft - startScroll) / 108);
        const visibleIndex = filtered.findIndex(p => pageKey(p) === pageKey(physical[position]));
        const target = filtered[Math.max(0, Math.min(filtered.length - 1, visibleIndex + delta))];
        if (target) move(physical.findIndex(p => pageKey(p) === pageKey(target)));
      };
      const cleanup = () => { window.removeEventListener("pointermove", drag); window.removeEventListener("pointerup", done); window.removeEventListener("pointercancel", cancel); };
      const done = () => { cleanup(); if (editing) save(plan, "boundary_move"); };
      const cancel = () => { cleanup(); if (editing) { plan = editing.plan; editing = null; render(); savedMessage(); } };
      window.addEventListener("pointermove", drag); window.addEventListener("pointerup", done, { once: true }); window.addEventListener("pointercancel", cancel, { once: true });
    }); card.append(button);
  }
  function savedMessage() { /** Только ACK позволяет назвать редакцию сохранённой. */ saveLabel.textContent = saved ? `Сохранено на сервере · редакция ${saved.plan_revision} · ${sourceLabel(saved.segmentation_source)}` : "Черновик ещё не создан"; saveLabel.classList.remove("boundary__save--pending"); }
  async function mutate(path, method, body) {
    /** При конфликте перечитывает актуальную редакцию без автоматической перезаписи. */
    if (busy) return null;
    busy = true; controls(); saveLabel.textContent = "Сохраняем на сервере…"; saveLabel.classList.add("boundary__save--pending");
    try { const response = await api(endpoint() + path, { method, body: JSON.stringify({ expected_revision: saved.plan_revision, ...body }) }); errorLabel.hidden = true; return response; }
    catch (error) { errorLabel.textContent = error.status === 409 ? "План изменён или уже заблокирован. Загружена актуальная версия; проверьте состав перед повтором." : error.message; errorLabel.hidden = false; notice(errorLabel.textContent, true); return null; }
    finally { busy = false; editing = null; saved = null; await changed(); savedMessage(); controls(); }
  }
  async function save(next, action) {
    /** Первая ручная правка одним PUT переключает режим и сохраняет весь состав. */
    const before = structuredClone(saved.draft_plan || plan);
    const previousExclusions = new Set(before.excluded_pages.map(pageKey));
    if (action === "boundary_move" && next.excluded_pages.some(p => !previousExclusions.has(pageKey(p))) && !window.confirm("Сдвиг оставит страницы вне диапазонов. Исключить их из PDF-комплектов, сохранив оригиналы и превью?")) { plan = before; editing = null; render(); savedMessage(); return; }
    const response = await mutate("/segmentation", "PUT", { ...next, action });
    if (response && action !== "undo") history.push(before); controls();
  }
  for (const control of [auto, skip]) control.addEventListener("change", async () => {
    if (control === skip && skip.checked) auto.checked = false;
    if (control === auto && auto.checked) skip.checked = false;
    if (snapshot) {
      if (saved?.segmentation_source.startsWith("manual") && !window.confirm("Сменить режим и восстановить исходный состав? Текущий ручной черновик будет заменён.")) { auto.checked = saved.auto_split; skip.checked = saved.skip_segmentation; return; }
      await mutate("/mode", "PATCH", flags()); history = [];
    }
    controls();
  });
  document.querySelectorAll("[data-boundary-action]").forEach(button => button.addEventListener("click", async () => {
    if (!plan || busy) return;
    const chosen = physical.filter(p => selected.has(pageKey(p))), ref = chosen[0] || physical[0], action = button.dataset.boundaryAction;
    try {
      if (action === "manual") await save(plan, "manual");
      if (action === "split") await save(splitRange(plan, ref, physical), "split");
      if (action === "merge") await save(mergeRanges(plan, ref, physical), "merge");
      if (action === "exclude" && chosen.length && window.confirm(`Исключить ${chosen.length} стр. из регистрационных PDF? Оригиналы и превью сохранятся. Пустота страниц автоматически не определялась.`)) await save(excludePages(plan, chosen), "exclude");
      if (action === "create") await save(createRange(plan, chosen, physical), "restore");
      if (action === "undo" && history.length) { const previous = history.at(-1), response = await mutate("/segmentation", "PUT", { ...previous, action: "undo" }); if (response) history.pop(); }
      if (action === "reset" && window.confirm("Восстановить первоначальный состав?")) await save(saved.initial_plan, "reset");
    } catch (error) { notice(error.message, true); }
  }));
  confirm.addEventListener("click", () => mutate("/segmentation/confirm", "POST", {}));
  sourceSelect.addEventListener("change", () => { scroll.scrollLeft = 0; render(); });
  document.querySelector("[data-page-jump]").addEventListener("change", e => {
    const index = Math.max(0, Math.min(physical.length - 1, Number(e.target.value) - 1)); if (!physical[index]) return;
    sourceSelect.value = ""; scroll.scrollLeft = Math.max(0, index * 108 - scroll.clientWidth / 2); selection(physical[index]); viewer.select(physical[index]);
  });
  scroll.addEventListener("scroll", render, { passive: true }); window.addEventListener("resize", render);
  return {
    flags, pending: () => busy || !!editing,
    expire() { /** Закрывает локальные действия и RAM-кеш сразу по TTL. */ if (snapshot) snapshot.status = "expired"; clearPreviews(); controls(); },
    result(value) {
      /** Показывает приложения ручных групп только после проверенного извлечения. */
      for (const file of value.files) for (const doc of file.documents) for (const attachment of doc.requisites.attachments) {
        const range = attachment.page_range;
        if (range?.length === 2) for (let n = range[0]; n <= range[1]; n++) finalRoles.set(pageKey({ file_id: file.file_id, page_number: n }), `Приложение: ${attachment.title || attachment.document_kind || "к документу"}`);
      }
      if (snapshot) viewer.update(snapshot, physical);
    },
    async update(data) {
      /** Snapshot без тысячи URL; состав запрашивается при смене редакции/состояния. */
      if (lastJob !== data.job_id) { generation++; lastJob = data.job_id; clearPreviews(); cache.clear(); finalRoles.clear(); history = []; saved = null; selected.clear(); scroll.scrollLeft = 0; errorLabel.hidden = true; }
      snapshot = data;
      physical = data.sources.flatMap(source => Array.from({ length: source.page_count || 0 }, (_, i) => ({ file_id: source.file_id, page_number: i + 1 })));
      if (!saved || saved.plan_revision !== data.plan_revision || saved.locked !== data.plan_locked || saved.preview_complete !== data.preview_complete || savedStatus !== data.status) {
        saved = await api(`${endpoint()}/segmentation`); savedStatus = data.status; plan = saved.draft_plan || { documents: [], excluded_pages: [] }; editing = null;
      }
      auto.checked = saved.auto_split; skip.checked = saved.skip_segmentation;
      const previousSource = sourceSelect.value; sourceSelect.replaceChildren(new Option("Все файлы", ""));
      for (const source of data.sources) sourceSelect.add(new Option(source.file_name, source.file_id)); sourceSelect.value = previousSource;
      document.querySelector("[data-page-counts]").textContent = `Превью ${data.pages_ready}/${data.pages_total} · в диапазонах ${saved.assigned_count} · исключено ${saved.excluded_count} · свободно ${saved.unassigned_count}`;
      document.querySelector("[data-page-jump]").max = physical.length || 1;
      document.querySelector("[data-boundary-status]").textContent = data.preview_complete ? `${saved.document_count} диапазонов · ${data.pages_total} страниц · ${sourceLabel(saved.segmentation_source)}` : `Подготовка превью: ${data.pages_ready}/${data.pages_total || "…"}. Готовые страницы уже доступны для разметки.`;
      if (!editing && !busy) savedMessage(); render(); viewer.update(data, physical); return { ...data, segmentation: saved };
    },
  };
}
