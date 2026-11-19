// app/web/static/js/features/history.js
/** Owner-история из REST: безопасная пагинация и восстановление без отмены фоновых заданий. */
import { api } from '../api.js';

export function initializeHistory(openJob) {
  /** Загружает сводки только при открытии диалога; имена файлов вставляются как текст. */
  const dialog = document.querySelector('[data-history]'), list = document.querySelector('[data-history-list]');
  const status = document.querySelector('[data-history-status]'), more = document.querySelector('[data-history-more]');
  const labels = { uploaded:'Готово к анализу',queued:'В очереди',running:'Обрабатывается',awaiting_confirmation:'Требуется подтверждение',completed:'Результат готов',completed_with_warnings:'Готово с предупреждениями',failed:'Ошибка',cancelled:'Отменено' };
  let offset = 0, busy = false, generation = 0;
  async function load(reset = false) {
    /** Переоткрытие не позволяет позднему ответу дописать старую страницу истории. */
    if (busy && !reset) return;
    if (reset) { generation++; offset = 0; list.replaceChildren(); }
    const own = generation; busy = true; more.disabled = true; status.textContent = 'Загружаем задания…';
    try {
      const data = await api(`/api/v1/jobs?offset=${offset}&limit=20`);
      if (own !== generation) return;
      for (const job of data.jobs) {
        const item = document.createElement('li'); item.className = 'history__item';
        const button = document.createElement('button'); button.type = 'button'; button.className = 'button';
        button.textContent = job.files.join(', ') || 'Пакет документов';
        const meta = document.createElement('span'); meta.className = 'history__meta';
        meta.textContent = (labels[job.status] || job.status) + ' · создано ' + new Date(job.created_at).toLocaleString('ru-RU') + (job.expires_at ? ' · удаление ' + new Date(job.expires_at).toLocaleString('ru-RU') : ' · обрабатывается');
        button.addEventListener('click', async () => { if (await openJob(job.job_id)) dialog.close(); });
        item.append(button, meta); list.append(item);
      }
      offset += data.jobs.length; more.hidden = !data.has_more;
      status.textContent = list.children.length ? 'Откройте задание для продолжения' : 'Заданий пока нет';
    } catch (error) { if (own === generation) status.textContent = error.message; }
    finally { if (own === generation) { busy = false; more.disabled = false; } }
  }
  document.querySelector('[data-history-open]').addEventListener('click', () => { dialog.showModal(); load(true); });
  document.querySelector('[data-history-close]').addEventListener('click', () => dialog.close());
  more.addEventListener('click', () => load());
}
