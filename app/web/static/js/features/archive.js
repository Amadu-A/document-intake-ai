// app/web/static/js/features/archive.js
/** Подтверждение состава и одно скачивание ZIP после готовности типов/реквизитов; большие файлы не собираются в Blob. */
import { api, notice } from '../api.js';

export function createArchive(button, confirm, exported) {
  /** Хранит намерение текущей вкладки; смена пакета/TTL отзывает запрос и позднее скачивание. */
  let snapshot = null, blocked = true, pending = null, preparing = false, generation = 0;
  let controller = new AbortController();
  const storage = 'intake_archive_job';
  const errorLabel = document.querySelector('[data-archive-error]');
  try { pending = sessionStorage.getItem(storage); } catch { /* Storage не обязателен. */ }
  function remember(id) {
    /** В storage остаётся только ID пакета, без реквизитов и capability-токенов. */
    pending = id;
    try { if (id) sessionStorage.setItem(storage, id); else sessionStorage.removeItem(storage); } catch { /* Продолжаем в памяти вкладки. */ }
  }
  function paint() {
    /** Одна кнопка доступна рядом с confirm, во время ожидания и для повторного скачивания. */
    const success = ['completed', 'completed_with_warnings'].includes(snapshot?.status);
    const eligible = snapshot?.segmentation?.can_confirm || snapshot?.plan_locked || success;
    button.hidden = !eligible || ['failed', 'cancelled', 'expired'].includes(snapshot?.status);
    button.disabled = blocked || preparing || !!pending;
    button.textContent = preparing ? 'Собираем ZIP…' : pending ? 'Ожидаем документы для ZIP…' : 'Разрезать и скачать';
  }
  async function download() {
    /** Подготавливает на сервере и передаёт подписанный ZIP штатному загрузчику браузера. */
    if (preparing || !snapshot || !['completed', 'completed_with_warnings'].includes(snapshot.status)) return;
    const own = generation, id = snapshot.job_id; preparing = true; paint();
    try {
      const file = await api(`/api/v1/jobs/${id}/archive`, { method: 'POST', signal: controller.signal });
      if (own !== generation || snapshot?.job_id !== id || blocked || snapshot.expires_at && Date.parse(snapshot.expires_at) <= Date.now()) return;
      const link = document.createElement('a'); link.href = file.file_url; link.download = file.file_name;
      document.body.append(link); link.click(); link.remove(); remember(null); exported();
      notice('Архив передан браузеру для скачивания');
    } catch (error) {
      if (own === generation && error.name !== 'AbortError') { remember(null); errorLabel.textContent = error.message; errorLabel.hidden = false; notice(error.message, true); }
    } finally { if (own === generation) { preparing = false; paint(); } }
  }
  button.addEventListener('click', async () => {
    if (blocked || preparing || pending || !snapshot) return;
    const own = generation, id = snapshot.job_id; errorLabel.hidden = true; remember(id); paint();
    if (!snapshot.plan_locked) {
      const response = await confirm();
      if (own !== generation || snapshot?.job_id !== id) return;
      if (!response) { remember(null); paint(); return; }
    }
    if (own === generation) { paint(); if (pending === id) download(); }
  });
  return {
    update(data, unavailable) {
      /** SSE/polling запускают ZIP только для пакета, скачивание которого запросили явно. */
      snapshot = data; blocked = unavailable;
      if (pending && data && data.job_id !== pending || data && ['failed', 'cancelled', 'expired'].includes(data.status)) remember(null);
      paint(); if (!blocked && pending === data?.job_id) download();
    },
    reset() {
      /** Отмена предыдущего поколения предотвращает автоматическую загрузку чужого нового пакета. */
      generation++; controller.abort(); controller = new AbortController(); snapshot = null;
      preparing = false; remember(null); errorLabel.hidden = true; paint();
    },
  };
}
