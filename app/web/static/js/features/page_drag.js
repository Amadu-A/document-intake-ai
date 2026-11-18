// app/web/static/js/features/page_drag.js
/** Мышиный перенос поверх виртуальной ленты: вставка, автопрокрутка, Escape; сохранение делегировано редактору. */
export function createPageDrag(scroll, track, context, begin, commit, announce) {
  /** Pointer-жест не зависит от жизни DOM-карточки при виртуализации и не затрагивает ↔ границы. */
  let gesture = null, frame = null, suppressUntil = 0;
  function clear() {
    /** Убирает только локальный жест; несохранённый порядок на сервер не отправляется. */
    const active = gesture?.active; gesture = null; cancelAnimationFrame(frame); frame = null;
    if (active) suppressUntil = performance.now() + 200;
    paint();
  }
  function destination() {
    /** Выбирает половину видимой карточки; внешняя область и выбранные листы не являются целью. */
    if (!gesture?.active) return;
    const bounds = scroll.getBoundingClientRect(), state = context();
    if (!state.enabled || state.stamp !== gesture.stamp) { clear(); return; }
    gesture.target = null;
    if (gesture.x >= bounds.left && gesture.x <= bounds.right && gesture.y >= bounds.top && gesture.y <= bounds.bottom) {
      const cards = Array.from(track.querySelectorAll('[data-page-key]'));
      const card = cards.find(c => { const r = c.getBoundingClientRect(); return gesture.x >= r.left - 4 && gesture.x <= r.right + 4; });
      if (card && !gesture.keys.has(card.dataset.pageKey)) {
        gesture.target = state.pages.find(p => state.key(p) === card.dataset.pageKey);
        gesture.side = gesture.x < card.getBoundingClientRect().left + card.clientWidth / 2 ? 'before' : 'after';
      }
    }
    paint();
  }
  function paint() {
    /** Восстанавливает маркер вставки и подсветку после каждого virtual render. */
    for (const card of track.querySelectorAll('[data-page-key]')) {
      const target = gesture?.target && context().key(gesture.target) === card.dataset.pageKey;
      card.classList.toggle('boundary__page--dragging', !!gesture?.active && gesture.keys.has(card.dataset.pageKey));
      card.classList.toggle('boundary__page--drop-before', !!target && gesture.side === 'before');
      card.classList.toggle('boundary__page--drop-after', !!target && gesture.side === 'after');
    }
    scroll.classList.toggle('boundary__scroll--dragging', !!gesture?.active);
  }
  function tick() {
    /** Автопрокрутка работает и при неподвижной мыши у края ленты. */
    if (!gesture?.active) return;
    const r = scroll.getBoundingClientRect();
    if (gesture.y >= r.top && gesture.y <= r.bottom) {
      if (gesture.x > r.right - 36 && gesture.x <= r.right + 20) scroll.scrollLeft += 14;
      if (gesture.x < r.left + 36 && gesture.x >= r.left - 20) scroll.scrollLeft -= 14;
    }
    destination(); if (gesture?.active) frame = requestAnimationFrame(tick);
  }
  scroll.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.pointerType !== 'mouse' || !event.target.closest('[data-page-drag]')) return;
    const state = context(), key = event.target.closest('[data-page-key]')?.dataset.pageKey;
    if (!state.enabled || !key) return;
    const page = state.pages.find(p => state.key(p) === key);
    if (page) gesture = { page, stamp: state.stamp, x: event.clientX, y: event.clientY, origin: event.clientX, originY: event.clientY, active: false };
  });
  scroll.addEventListener('dragstart', event => { if (event.target.closest('[data-page-drag]')) event.preventDefault(); });
  scroll.addEventListener('click', event => { if (performance.now() < suppressUntil) { event.preventDefault(); event.stopImmediatePropagation(); } }, true);
  window.addEventListener('pointermove', event => {
    if (!gesture) return;
    gesture.x = event.clientX; gesture.y = event.clientY;
    if (!gesture.active && Math.hypot(gesture.x - gesture.origin, gesture.y - gesture.originY) >= 6) {
      const state = context();
      if (!state.enabled || state.stamp !== gesture.stamp) { clear(); return; }
      gesture.active = true;
      gesture.pages = state.selected.some(p => state.key(p) === state.key(gesture.page)) ? state.selected : [gesture.page];
      gesture.keys = new Set(gesture.pages.map(state.key)); begin(gesture.page);
      announce(`Перенос ${gesture.pages.length} стр.: отпустите перед/после нужной миниатюры. Escape — отмена.`);
      frame = requestAnimationFrame(tick);
    }
    if (gesture?.active) { event.preventDefault(); destination(); }
  }, { passive: false });
  window.addEventListener('pointerup', () => {
    const current = gesture, state = context(); clear();
    if (!current?.active) return;
    if (state.enabled && state.stamp === current.stamp && current.target) commit(current.pages, current.target, current.side);
    else announce('Перенос отменён: отпустите лист у другой страницы в ленте.');
  });
  window.addEventListener('pointercancel', clear);
  window.addEventListener('blur', clear);
  window.addEventListener('keydown', event => { if (event.key === 'Escape' && gesture) { clear(); announce('Перенос отменён'); } });
  return { paint, reset: clear, active: () => !!gesture?.active };
}
