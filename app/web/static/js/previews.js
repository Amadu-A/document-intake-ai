// app/web/static/js/previews.js
/** Ограниченный RAM-кеш превью: общие для ленты/viewer blob URL, максимум четыре HTTP-запроса. */
import { request } from "./api.js";

const entries = new Map(), waiting = [];
let active = 0;

function prune() {
  /** Удаляет старые завершённые изображения; приватные данные не попадают в browser storage. */
  for (const [key, item] of entries) {
    if (entries.size < 96) break;
    if (!item.done) continue;
    URL.revokeObjectURL(item.url); entries.delete(key);
  }
}

function drain() {
  /** Обслуживает конечную очередь без параллельного скачивания сотен страниц. */
  while (active < 4 && waiting.length) {
    const item = waiting.shift(); active++;
    (async () => {
      try {
        const response = await request(item.source, { signal: item.controller.signal });
        if (!response.ok) { await response.body?.cancel(); throw new Error("Превью временно недоступно"); }
        const blob = await response.blob(); item.controller.signal.throwIfAborted();
        item.url = URL.createObjectURL(blob); item.done = true; item.resolve(item.url);
      } catch (error) { if (entries.get(item.key) === item) entries.delete(item.key); item.reject(error); }
      finally { active--; drain(); }
    })();
  }
}

export function previewURL(source) {
  /** Объединяет одинаковые запросы независимо от обновившейся подписи URL; кеш живёт одну минуту. */
  const key = new URL(source, location.href).pathname, previous = entries.get(key);
  if (previous && Date.now() - previous.time < 60000) { entries.delete(key); entries.set(key, previous); return previous.promise; }
  if (previous?.done) { URL.revokeObjectURL(previous.url); entries.delete(key); }
  else if (previous) return previous.promise;
  prune();
  if (entries.size >= 96) return Promise.reject(new Error("Очередь превью занята"));
  const item = { key, source, time: Date.now(), done: false, controller: new AbortController() };
  item.promise = new Promise((resolve, reject) => { item.resolve = resolve; item.reject = reject; });
  entries.set(key, item); waiting.push(item); drain(); return item.promise;
}

export function clearPreviews() {
  /** Отменяет HTTP и отзывает все blob URL при смене задания или истечении TTL. */
  for (const item of entries.values()) { item.controller.abort(); if (item.url) URL.revokeObjectURL(item.url); }
  for (const item of waiting.splice(0)) item.reject(new DOMException("Задание закрыто", "AbortError"));
  entries.clear();
}
