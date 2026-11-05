// app/web/static/js/features/viewer.js
/** Просмотр физических страниц; обновления SSE сохраняют выбранную пользователем страницу. */
export function createViewer() {
  /** Возвращает API обновления рендеров и переходов без изменения VLM-плана. */
  let pages = [], sources = [], selected = 0, plan = null, zoom = 100, localURL = null;
  const image = document.querySelector("[data-page-image]");
  const empty = document.querySelector("[data-viewer-empty]");
  const local = document.querySelector("[data-local-pdf]");
  const thumbnails = document.querySelector("[data-thumbnails]");
  function show() {
    /** Рисует текущую страницу; принадлежность основному тексту/приложению берёт из VLM-плана. */
    if (!pages.length) return;
    image.src = pages[selected].preview_url;
    image.hidden = false; local.hidden = true; empty.hidden = true;
    image.alt = "Страница " + pages[selected].page_number + " исходного документа";
    document.querySelector("[data-page-label]").textContent = `Страница ${selected + 1} из ${pages.length}`;
    thumbnails.querySelectorAll("button").forEach((button, index) => {
      if (index === selected) button.setAttribute("aria-current", "page");
      else button.removeAttribute("aria-current");
    });
    const current = pages[selected];
    let role = "Принадлежность определяется VLM";
    for (const doc of plan?.documents || []) {
      if (doc.main_pages.some(p => p.file_id === current.file_id && p.page_number === current.page_number)) role = "Основной документ " + doc.document_index;
      for (const attachment of doc.attachments) {
        if (attachment.pages.some(p => p.file_id === current.file_id && p.page_number === current.page_number)) role = "Приложение: " + attachment.kind;
      }
    }
    const original = sources.find(source => source.file_id === current.file_id);
    document.querySelector("[data-page-role]").textContent = role + (original ? " · " + original.file_name : "");
    if (original) {
      const link = document.querySelector("[data-original]");
      link.hidden = false; link.href = original.file_url;
    }
  }
  function setZoom(value) {
    /** Выбирает ограниченную ступень масштаба; CSS остаётся отдельным модулем. */
    zoom = value;
    image.className = "viewer__image" + (zoom === 100 ? "" : " viewer__image--zoom-" + zoom);
    document.querySelector("[data-fit]").textContent = zoom + "%";
  }
  document.querySelector("[data-previous]").addEventListener("click", () => { selected = Math.max(0, selected - 1); show(); });
  document.querySelector("[data-next]").addEventListener("click", () => { selected = Math.min(pages.length - 1, selected + 1); show(); });
  document.querySelector("[data-fit]").addEventListener("click", () => setZoom(100));
  const levels = [75, 100, 125, 150, 200];
  document.querySelector("[data-zoom-in]").addEventListener("click", () => setZoom(levels[Math.min(4, levels.indexOf(zoom) + 1)]));
  document.querySelector("[data-zoom-out]").addEventListener("click", () => setZoom(levels[Math.max(0, levels.indexOf(zoom) - 1)]));
  return {
    update(snapshot) {
      /** Перестраивает миниатюры только при изменении набора страниц; ручной выбор сохраняется. */
      plan = snapshot.plan;
      sources = snapshot.sources || [];
      const incoming = snapshot.pages || [];
      if (incoming.length !== pages.length || incoming.some((p, i) => p.file_id !== pages[i]?.file_id)) {
        pages = incoming; selected = Math.max(0, Math.min(selected, pages.length - 1));
        thumbnails.replaceChildren();
        pages.forEach((page, index) => {
          const button = document.createElement("button"); button.type = "button";
          button.className = "viewer__thumbnail"; button.setAttribute("aria-label", "Страница " + (index + 1));
          const thumbnail = document.createElement("img"); thumbnail.src = page.preview_url; thumbnail.alt = ""; thumbnail.loading = "lazy";
          const number = document.createElement("span"); number.textContent = index + 1;
          button.append(thumbnail, number); button.addEventListener("click", () => { selected = index; show(); });
          thumbnails.append(button);
        });
        show();
      } else { pages = incoming; if (pages.length) show(); }
      const original = document.querySelector("[data-original]");
      const source = snapshot.sources?.find(s => s.file_id === pages[selected]?.file_id) || snapshot.sources?.[0];
      if (source) { original.hidden = false; original.href = source.file_url; }
    },
    select(ref) {
      /** Переходит к странице из карточки приложения или предупреждения. */
      const index = pages.findIndex(p => p.file_id === ref.file_id && p.page_number === ref.page_number);
      if (index >= 0) { selected = index; show(); }
    },
    local(file) {
      /** Даёт предварительный локальный просмотр PDF/изображения до запуска серверной обработки. */
      if (localURL) URL.revokeObjectURL(localURL);
      if (!file) return;
      localURL = URL.createObjectURL(file); pages = []; thumbnails.replaceChildren();
      if (file.type === "application/pdf") { local.data = localURL; local.hidden = false; image.hidden = true; empty.hidden = true; }
      else if (["image/jpeg", "image/png"].includes(file.type)) { image.src = localURL; image.hidden = false; local.hidden = true; empty.hidden = true; }
      else { local.hidden = true; image.hidden = true; empty.hidden = false; empty.textContent = "Рендер появится при обработке на сервере"; }
    },
  };
}
