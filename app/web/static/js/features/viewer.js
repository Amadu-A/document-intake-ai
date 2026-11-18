// app/web/static/js/features/viewer.js
/** Просмотр по PageRef с загрузкой только выбранного окна миниатюр. */
import { pageKey } from "./boundary_state.js";
import { previewURL } from "../previews.js";

export function createViewer() {
  /** Сохраняет физический выбор при progressive preview, polling и обновлении плана. */
  let pages = [], sources = [], selected = 0, zoom = 100, localURL = null, sequence = 0;
  let rotationJob = null, displayedKey = null;
  const rotations = new Map(), rotationStorage = "intake_view_rotations";
  let load = async p => p, onSelect = () => {}, role = () => "Физическая страница";
  const image = document.querySelector("[data-page-image]"), empty = document.querySelector("[data-viewer-empty]");
  const local = document.querySelector("[data-local-pdf]"), thumbnails = document.querySelector("[data-thumbnails]");
  const canvas = document.querySelector("[data-viewer-canvas]"), surface = document.querySelector("[data-page-surface]");
  const rotateButtons = Array.from(document.querySelectorAll("[data-rotate-left], [data-rotate-right]"));
  function useRotationJob(id) {
    /** Хранит только углы текущего пакета в этой вкладке, без изображений или OCR. */
    if (id === rotationJob) return;
    rotationJob = id; rotations.clear();
    try {
      const saved = JSON.parse(sessionStorage.getItem(rotationStorage) || "null"), allowed = new Set(pages.map(pageKey));
      if (saved?.job_id === id) for (const [key, value] of Object.entries(saved.angles || {})) if (allowed.has(key) && [90, 180, 270].includes(value)) rotations.set(key, value);
    } catch { /* Недоступное browser storage не блокирует просмотр. */ }
  }
  function layout() {
    /** Учитывает повёрнутые габариты: вся страница помещается в 100%, при zoom доступен скролл. */
    const angle = pages[selected] ? rotations.get(pageKey(pages[selected])) || 0 : 0;
    const label = document.querySelector("[data-rotation-label]");
    if (label.textContent !== angle + "°") label.textContent = angle + "°";
    const ready = !image.hidden && image.complete && image.naturalWidth > 0 && (!pages[selected] || displayedKey === pageKey(pages[selected]));
    for (const button of rotateButtons) button.disabled = !ready || !pages[selected];
    surface.hidden = !ready;
    if (!ready) return;
    const style = getComputedStyle(canvas), width = Math.max(1, canvas.clientWidth - parseFloat(style.paddingLeft) - parseFloat(style.paddingRight));
    const sideways = angle === 90 || angle === 270, naturalWidth = image.naturalWidth, naturalHeight = image.naturalHeight;
    const scale = width / (sideways ? naturalHeight : naturalWidth) * zoom / 100;
    for (const [name, value] of Object.entries({ "--image-width": naturalWidth * scale + "px", "--image-height": naturalHeight * scale + "px", "--surface-width": (sideways ? naturalHeight : naturalWidth) * scale + "px", "--surface-height": (sideways ? naturalWidth : naturalHeight) * scale + "px", "--page-angle": angle + "deg" })) surface.style.setProperty(name, value);
  }
  function rotate(delta) {
    /** Поворот относится к конкретному PageRef и сохраняется при навигации и reload. */
    const ref = pages[selected]; if (!ref || image.hidden || !image.complete) return;
    const key = pageKey(ref), value = ((rotations.get(key) || 0) + delta + 360) % 360;
    if (value) rotations.set(key, value); else rotations.delete(key);
    try { sessionStorage.setItem(rotationStorage, JSON.stringify({ job_id: rotationJob, angles: Object.fromEntries(rotations) })); } catch { /* Просмотр работает и без storage. */ }
    layout(); canvas.scrollTop = 0; canvas.scrollLeft = 0;
  }
  image.addEventListener("load", layout);
  image.addEventListener("error", () => { surface.hidden = true; for (const button of rotateButtons) button.disabled = true; });
  let canvasWidth = 0;
  new ResizeObserver(() => { if (canvas.clientWidth !== canvasWidth) { canvasWidth = canvas.clientWidth; layout(); } }).observe(canvas);
  document.querySelector("[data-rotate-left]").addEventListener("click", () => rotate(-90));
  document.querySelector("[data-rotate-right]").addEventListener("click", () => rotate(90));
  async function show() {
    /** Ответ старого запроса не переключает страницу после нового выбора. */
    if (!pages.length) return;
    const token = ++sequence, ref = pages[selected]; onSelect(ref);
    if (displayedKey !== pageKey(ref)) { image.hidden = true; empty.hidden = false; empty.textContent = "Загружаем превью…"; layout(); }
    document.querySelector("[data-page-label]").textContent = `Страница ${selected + 1} из ${pages.length}`;
    const original = sources.find(s => s.file_id === ref.file_id);
    document.querySelector("[data-page-role]").textContent = role(ref) + (original ? " · " + original.file_name : "");
    if (original) { const link = document.querySelector("[data-original]"); link.hidden = false; link.href = original.file_url; }
    const begin = Math.max(0, selected - 5), windowPages = pages.slice(begin, begin + 11);
    thumbnails.replaceChildren();
    for (const [i, page] of windowPages.entries()) {
      const button = document.createElement("button"); button.type = "button"; button.className = "viewer__thumbnail";
      button.setAttribute("aria-label", "Страница " + (begin + i + 1));
      if (begin + i === selected) button.setAttribute("aria-current", "page");
      const number = document.createElement("span"); number.textContent = begin + i + 1; button.append(number);
      button.addEventListener("click", () => { selected = begin + i; show(); }); thumbnails.append(button);
      load(page).then(async meta => {
        if (token !== sequence || !meta?.thumbnail_url) return;
        const url = await previewURL(meta.thumbnail_url); if (token !== sequence) return;
        const thumb = document.createElement("img"); thumb.src = url; thumb.alt = ""; thumb.loading = "lazy"; button.prepend(thumb);
      }).catch(() => {});
    }
    try {
      const meta = await load(ref); if (token !== sequence) return;
      local.hidden = true; image.hidden = !meta?.preview_url; empty.hidden = !!meta?.preview_url;
      if (meta?.preview_url) {
        const url = await previewURL(meta.preview_url); if (token !== sequence) return;
        if (image.getAttribute("src") !== url) image.src = url;
        displayedKey = pageKey(ref);
        image.alt = "Страница " + ref.page_number + " исходного документа";
      } else empty.textContent = "Превью этой страницы ещё готовится…";
      layout();
    } catch { if (token === sequence) { image.hidden = true; empty.hidden = false; empty.textContent = "Превью временно недоступно"; layout(); } }
  }
  function setZoom(value) {
    /** Ограничивает ступени масштаба CSS-классами. */
    zoom = value; image.className = "viewer__image" + (zoom === 100 ? "" : " viewer__image--zoom-" + zoom);
    document.querySelector("[data-fit]").textContent = zoom + "%";
    layout();
  }
  document.querySelector("[data-previous]").addEventListener("click", () => { selected = Math.max(0, selected - 1); show(); });
  document.querySelector("[data-next]").addEventListener("click", () => { selected = Math.min(pages.length - 1, selected + 1); show(); });
  document.querySelector("[data-fit]").addEventListener("click", () => setZoom(100));
  const levels = [75, 100, 125, 150, 200];
  document.querySelector("[data-zoom-in]").addEventListener("click", () => setZoom(levels[Math.min(4, levels.indexOf(zoom) + 1)]));
  document.querySelector("[data-zoom-out]").addEventListener("click", () => setZoom(levels[Math.max(0, levels.indexOf(zoom) - 1)]));
  return {
    reset() {
      /** Отзывает локальный PDF и не позволяет старому show изменить новый экран. */
      sequence++; if (localURL) URL.revokeObjectURL(localURL); localURL = null;
      pages = []; sources = []; selected = 0; rotationJob = displayedKey = null; rotations.clear();
      try { sessionStorage.removeItem(rotationStorage); } catch { /* Хранилище необязательно. */ }
      setZoom(100); thumbnails.replaceChildren();
      image.removeAttribute("src"); image.alt = ""; image.hidden = true;
      local.removeAttribute("data"); local.hidden = true; empty.hidden = false; empty.textContent = "Загрузите документы для просмотра";
      document.querySelector("[data-page-label]").textContent = "Страницы не загружены";
      document.querySelector("[data-page-role]").textContent = "";
      const link = document.querySelector("[data-original]"); link.hidden = true; link.removeAttribute("href");
      layout();
    },
    configure(loader, select, describe) { /** Подключает пагинацию и общий выбор редактора. */ load = loader; onSelect = select; role = describe; },
    update(snapshot, refs) {
      /** Применяет метаданные без сброса выбранной физической страницы. */
      const previous = pages[selected]; sources = snapshot.sources || []; pages = refs || snapshot.pages || [];
      useRotationJob(snapshot.job_id);
      const found = previous ? pages.findIndex(p => pageKey(p) === pageKey(previous)) : 0;
      selected = Math.max(0, found); show();
    },
    select(ref) { /** Переходит по паре source/page. */ const index = pages.findIndex(p => pageKey(p) === pageKey(ref)); if (index >= 0) { selected = index; show(); } },
    local(file) {
      /** До серверного превью не открывает тяжёлый PDF целиком в браузере. */
      if (localURL) URL.revokeObjectURL(localURL);
      pages = []; displayedKey = null; thumbnails.replaceChildren(); sequence++;
      if (!file || file.size > 25 * 1024 * 1024) { local.hidden = true; image.hidden = true; empty.hidden = false; empty.textContent = "Готовим серверные превью…"; layout(); return; }
      localURL = URL.createObjectURL(file);
      if (file.type === "application/pdf") { local.data = localURL; local.hidden = false; image.hidden = true; empty.hidden = true; }
      else if (["image/jpeg", "image/png"].includes(file.type)) { image.src = localURL; image.hidden = false; local.hidden = true; empty.hidden = true; }
      else { local.hidden = true; image.hidden = true; empty.hidden = false; empty.textContent = "Готовим серверные превью…"; }
      layout();
    },
  };
}
