// app/web/static/js/components/tabs.js
/** Доступные вкладки с native buttons, roving tabindex и стрелками клавиатуры. */
export function tabs(elements, activate) {
  /** Вызывает обработчик выбранного таба; переключает focus при ArrowLeft/ArrowRight. */
  const buttons = [...elements];
  buttons.forEach((button, index) => {
    button.addEventListener("click", () => choose(index));
    button.addEventListener("keydown", event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 :
        (index + (event.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
      choose(next);
      buttons[next].focus();
    });
  });
  function choose(index) {
    /** Синхронизирует ARIA и состояние feature без зависимости от CSS-классов. */
    buttons.forEach((button, i) => {
      button.setAttribute("aria-selected", String(i === index));
      button.tabIndex = i === index ? 0 : -1;
    });
    activate(buttons[index]);
  }
}
