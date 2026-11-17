// app/web/static/js/features/modes.js
/** Единственное преобразование выбранной радиокарточки в существующий REST-контракт. */
export function modeFlags(mode) {
  /** Независимых переключателей нет: каждый режим однозначно задаёт оба параметра. */
  return { auto_split: mode === "auto", skip_segmentation: mode === "source" };
}

export function modeFromFlags(flags) {
  /** Восстанавливает выбранную карточку по авторитетному серверному снимку. */
  return flags.skip_segmentation ? "source" : flags.auto_split ? "auto" : "review";
}
