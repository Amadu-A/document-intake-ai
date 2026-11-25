// app/web/static/js/api.js
/** Общий REST-клиент: cookie/CSRF, единый envelope ошибок и скачивание экспорта. */
async function pause(milliseconds, signal) {
  /** Отмена задания прерывает Retry-After сразу, освобождая очередь превью для следующего пакета. */
  signal?.throwIfAborted();
  await new Promise((resolve, reject) => {
    const cleanup = () => signal?.removeEventListener("abort", abort);
    const timer = setTimeout(() => { cleanup(); resolve(); }, milliseconds);
    const abort = () => { clearTimeout(timer); cleanup(); reject(signal.reason || new DOMException("Запрос отменён", "AbortError")); };
    signal?.addEventListener("abort", abort, { once: true });
  });
}

export async function request(path, options = {}) {
  /** Ограниченно повторяет только чтение при 429; команды автоматически не повторяются. */
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (options.method && options.method !== "GET") {
    headers.set("X-CSRF-Token", document.querySelector('meta[name="csrf-token"]').content);
  }
  for (let attempt = 0; attempt < 3; attempt++) {
    const response = await fetch(path, { ...options, headers, credentials: "same-origin", cache: "no-store" });
    if (response.status !== 429 || !["GET", "HEAD"].includes(options.method || "GET") || attempt === 2) return response;
    const value = response.headers.get("Retry-After"), numeric = value && /^\d+$/.test(value);
    const delay = value ? (numeric ? Number(value) * 1000 : Date.parse(value) - Date.now()) : 1000 * (attempt + 1);
    if (!Number.isFinite(delay) || delay > 60000) return response;
    await response.body?.cancel();
    await pause(Math.max(0, delay), options.signal);
    options.signal?.throwIfAborted();
  }
}

export async function api(path, options = {}) {
  /** Посылает JSON-запрос текущей сессии; приватные ответы не сохраняет в console/log. */
  const response = await checkedRequest(path, options);
  return response.json();
}

async function checkedRequest(path, options) {
  /** Проверяет общий envelope для JSON/файлов, не сохраняя payload в console. */
  const response = await request(path, options);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    const error = new Error(data.error?.message || "Запрос не выполнен");
    error.code = data.error?.code;
    error.status = response.status;
    throw error;
  }
  return response;
}

export async function apiFile(path, options = {}) {
  /** Читает бинарный ответ с теми же cookie, отменой, 429 и обработкой ошибок. */
  const response = await checkedRequest(path, options);
  let name = 'Реестр документов.xlsx';
  const encoded = response.headers.get('Content-Disposition')?.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  try { if (encoded) name = decodeURIComponent(encoded); } catch { /* Сохраняем безопасное имя. */ }
  return { blob: await response.blob(), file_name: name };
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
