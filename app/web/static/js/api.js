// app/web/static/js/api.js
/** Общий REST-клиент: cookie/CSRF, единый envelope ошибок и скачивание экспорта. */
export async function api(path, options = {}) {
  /** Посылает запрос текущей сессии; приватные ответы не сохраняет в console/log. */
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") {
    headers.set("X-CSRF-Token", document.querySelector('meta[name="csrf-token"]').content);
  }
  const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.error?.message || "Запрос не выполнен");
    error.code = data.error?.code;
    error.status = response.status;
    throw error;
  }
  return data;
}

export function downloadJSON(value) {
  /** Создаёт локальный файл только из уже проверенного результата API. */
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "document-intake-result.json";
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function notice(message, error = false) {
  /** Отображает безопасный текст статуса без HTML-интерполяции. */
  const element = document.querySelector("[data-notice]");
  element.textContent = message;
  element.classList.toggle("notice--error", error);
}
