// app/web/static/js/features/viewer.js
/** Просмотр по PageRef с загрузкой только выбранного окна миниатюр. */
import { pageKey } from "./boundary_state.js";

export function createViewer() {
  /** Сохраняет физический выбор при progressive preview, polling и обновлении плана. */
  let pages = [], sources = [], selected = 0, zoom = 100, localURL = null, sequence = 0;
  let load = async p => p, onSelect = () => {}, role = () => "Физическая страница";
  const image = document.querySelector("[data-page-image]"), empty = document.querySelector("[data-viewer-empty]");
  const local = document.querySelector("[data-local-pdf]"), thumbnails = document.querySelector("[data-thumbnails]");
  async function show() {
    /** Ответ старого запроса не переключает страницу после нового выбора. */
    if (!pages.length) return;
    const token = ++sequence, ref = pages[selected]; onSelect(ref);
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
      load(page).then(meta => {
        if (token !== sequence || !meta?.thumbnail_url) return;
        const thumb = document.createElement("img"); thumb.src = meta.thumbnail_url; thumb.alt = ""; thumb.loading = "lazy"; button.prepend(thumb);
      }).catch(() => {});
    }
    try {
      const meta = await load(ref); if (token !== sequence) return;
      local.hidden = true; image.hidden = !meta?.preview_url; empty.hidden = !!meta?.preview_url;
      if (meta?.preview_url) {
        if (image.getAttribute("src") !== meta.preview_url) image.src = meta.preview_url;
        image.alt = "Страница " + ref.page_number + " исходного документа";
      } else empty.textContent = "Превью этой страницы ещё готовится…";
    } catch { if (token === sequence) { image.hidden = true; empty.hidden = false; empty.textContent = "Превью временно недоступно"; } }
  }
  function setZoom(value) {
    /** Ограничивает ступени масштаба CSS-классами. */
    zoom = value; image.className = "viewer__image" + (zoom === 100 ? "" : " viewer__image--zoom-" + zoom);
    document.querySelector("[data-fit]").textContent = zoom + "%";
  }
  document.querySelector("[data-previous]").addEventListener("click", () => { selected = Math.max(0, selected - 1); show(); });
  document.querySelector("[data-next]").addEventListener("click", () => { selected = Math.min(pages.length - 1, selected + 1); show(); });
  document.querySelector("[data-fit]").addEventListener("click", () => setZoom(100));
  const levels = [75, 100, 125, 150, 200];
  document.querySelector("[data-zoom-in]").addEventListener("click", () => setZoom(levels[Math.min(4, levels.indexOf(zoom) + 1)]));
  document.querySelector("[data-zoom-out]").addEventListener("click", () => setZoom(levels[Math.max(0, levels.indexOf(zoom) - 1)]));
  return {
    configure(loader, select, describe) { /** Подключает пагинацию и общий выбор редактора. */ load = loader; onSelect = select; role = describe; },
    update(snapshot, refs) {
      /** Применяет метаданные без сброса выбранной физической страницы. */
      const previous = pages[selected]; sources = snapshot.sources || []; pages = refs || snapshot.pages || [];
      const found = previous ? pages.findIndex(p => pageKey(p) === pageKey(previous)) : 0;
      selected = Math.max(0, found); show();
    },
    select(ref) { /** Переходит по паре source/page. */ const index = pages.findIndex(p => pageKey(p) === pageKey(ref)); if (index >= 0) { selected = index; show(); } },
    local(file) {
      /** До серверного превью не открывает тяжёлый PDF целиком в браузере. */
      if (localURL) URL.revokeObjectURL(localURL);
      pages = []; thumbnails.replaceChildren(); sequence++;
      if (!file || file.size > 25 * 1024 * 1024) { local.hidden = true; image.hidden = true; empty.hidden = false; empty.textContent = "Готовим серверные превью…"; return; }
      localURL = URL.createObjectURL(file);
      if (file.type === "application/pdf") { local.data = localURL; local.hidden = false; image.hidden = true; empty.hidden = true; }
      else if (["image/jpeg", "image/png"].includes(file.type)) { image.src = localURL; image.hidden = false; local.hidden = true; empty.hidden = true; }
      else { local.hidden = true; image.hidden = true; empty.hidden = false; empty.textContent = "Готовим серверные превью…"; }
    },
  };
}
