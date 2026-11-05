// app/web/static/js/app.js
/** Точка сборки feature-модулей; бизнес-решения и состояние заданий остаются на сервере. */
import { initializePrompts } from "./features/prompts.js";
import { createViewer } from "./features/viewer.js";
import { initializeAnalysis } from "./features/analysis.js";
import { tabs } from "./components/tabs.js";

initializePrompts();
initializeAnalysis(createViewer());
tabs(document.querySelectorAll("[data-result-tab]"), button => {
  /** Открывает выбранное представление результата без дублирования данных API. */
  document.querySelectorAll("[data-result-panel]").forEach(panel => {
    panel.hidden = panel.dataset.resultPanel !== button.dataset.resultTab;
  });
});
