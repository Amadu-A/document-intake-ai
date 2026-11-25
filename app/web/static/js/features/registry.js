// app/web/static/js/features/registry.js
/** Скачивание готового реестра; не подтверждает план и не запускает обработку документов. */
import { apiFile, notice } from '../api.js';

export function createRegistry(button) {
  /** Поколение пакета и AbortController защищают от позднего скачивания старого ответа. */
  let snapshot = null, unavailable = true, pending = false, generation = 0;
  let controller = new AbortController(), blob = null, timer = null;
  const hint = document.querySelector('[data-registry-hint]');
  const warning = document.querySelector('[data-registry-warning]');
  const errorLabel = document.querySelector('[data-registry-error]');
  const success = () => ['completed', 'completed_with_warnings'].includes(snapshot?.status);
  function revoke() {
    /** Освобождает blob экспорта; факты не записываются в sessionStorage/localStorage. */
    clearTimeout(timer); timer = null;
    if (blob) URL.revokeObjectURL(blob); blob = null;
  }
  function invalidate() {
    /** Отзывает чтение при смене пакета/TTL до позднего response и native click. */
    generation++; controller.abort(); controller = new AbortController(); pending = false; revoke();
  }
  function paint() {
    /** Доступность следует terminal snapshot; предупреждение не становится колонкой Excel. */
    button.hidden = !snapshot;
    button.disabled = unavailable || pending || !success();
    button.textContent = pending ? 'Формируем реестр…' : 'Скачать реестр документов';
    hint.hidden = !snapshot || success();
    const incomplete = Object.values(snapshot?.document_processing || {}).filter(p => p.status !== 'recognized').length;
    warning.hidden = !success() || !incomplete && snapshot.status !== 'completed_with_warnings';
    warning.textContent = incomplete ? `Блоков с неполным распознаванием: ${incomplete}. Непроверенные ячейки будут пустыми.` : 'Результат содержит предупреждения. Проверьте реквизиты перед регистрацией.';
  }
  button.addEventListener('click', async () => {
    if (unavailable || pending || !success()) return;
    const own = generation, id = snapshot.job_id; pending = true; errorLabel.hidden = true; paint();
    try {
      const file = await apiFile(`/api/v1/jobs/${id}/registry.xlsx`, { signal: controller.signal });
      if (own !== generation || snapshot?.job_id !== id || unavailable || !success() || snapshot.expires_at && Date.parse(snapshot.expires_at) <= Date.now()) return;
      revoke(); blob = URL.createObjectURL(file.blob);
      const link = document.createElement('a'); link.href = blob; link.download = file.file_name;
      document.body.append(link); link.click(); link.remove(); timer = setTimeout(revoke, 1000);
      notice('Реестр документов передан браузеру');
    } catch (error) {
      if (own === generation && error.name !== 'AbortError') {
        errorLabel.textContent = error.message; errorLabel.hidden = false; notice(error.message, true);
      }
    } finally { if (own === generation) { pending = false; paint(); } }
  });
  return {
    update(data, blocked) {
      /** Reload/история восстанавливают кнопку, без автоматического запуска экспорта. */
      if (snapshot?.job_id !== data?.job_id || blocked && pending) { invalidate(); errorLabel.hidden = true; }
      snapshot = data; unavailable = blocked; paint();
    },
    reset() {
      /** Новый пакет освобождает запросы и URL предыдущего поколения. */
      invalidate(); snapshot = null; unavailable = true; errorLabel.hidden = true; paint();
    },
  };
}
