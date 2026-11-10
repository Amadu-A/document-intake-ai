// app/web/static/js/features/boundary_state.js
/** Чистые операции редактора над физическими PageRef; сервер повторно валидирует каждую запись. */
export const pageKey = ref => `${ref.file_id}:${ref.page_number}`;
export const sourceLabel = source => ({ source_files: "Файлы задали границы", vlm_auto: "VLM разделила", vlm_proposed: "VLM предложила", vlm_confirmed: "Предложение VLM подтверждено", manual: "Размечено пользователем", manual_override: "Скорректировано вручную" })[source] || "Исходная разметка";

export function newGroupId() {
  /** Создаёт несекретный клиентский ID, доступный также на LAN HTTP без crypto.randomUUID. */
  return "group-" + Array.from(crypto.getRandomValues(new Uint8Array(8)), n => n.toString(16).padStart(2, "0")).join("");
}

export function groupFor(plan, ref) {
  /** Находит только точное назначение пары source/page, не сравнивая номера разных источников. */
  return plan?.documents.find(d => d.pages.some(p => pageKey(p) === pageKey(ref)));
}

export function splitRange(plan, ref) {
  /** Делит после выбранной страницы; обе получившиеся группы обязаны быть непустыми. */
  const next = structuredClone(plan), group = groupFor(next, ref);
  if (!group) throw new Error("Выберите страницу регистрационного диапазона");
  const at = group.pages.findIndex(p => pageKey(p) === pageKey(ref)) + 1;
  if (at >= group.pages.length) throw new Error("После выбранной страницы диапазон уже заканчивается");
  const index = next.documents.indexOf(group);
  next.documents.splice(index + 1, 0, { client_id: newGroupId(), pages: group.pages.splice(at) });
  return next;
}

export function mergeRanges(plan, ref, physical) {
  /** Объединяет соседние группы списка; исключённые страницы между ними не добавляются. */
  const next = structuredClone(plan), group = groupFor(next, ref);
  const index = next.documents.indexOf(group), other = next.documents[index + 1];
  if (!group || !other) throw new Error("Следующего регистрационного диапазона нет");
  const order = new Map(physical.map((p, i) => [pageKey(p), i]));
  group.pages = [...group.pages, ...other.pages].sort((a, b) => order.get(pageKey(a)) - order.get(pageKey(b)));
  next.documents.splice(index + 1, 1);
  return next;
}

export function excludePages(plan, selected) {
  /** Убирает явно выбранные страницы из регистраций, сохраняя их как явные исключения. */
  const next = structuredClone(plan), keys = new Set(selected.map(pageKey));
  for (const document of next.documents) document.pages = document.pages.filter(p => !keys.has(pageKey(p)));
  next.documents = next.documents.filter(d => d.pages.length);
  const excluded = new Set(next.excluded_pages.map(pageKey));
  for (const page of selected) if (!excluded.has(pageKey(page))) next.excluded_pages.push({ ...page });
  return next;
}

export function createRange(plan, selected, physical) {
  /** Назначает свободные/исключённые страницы новой группе в исходном физическом порядке. */
  if (!selected.length || selected.some(p => groupFor(plan, p))) throw new Error("Выберите страницы вне диапазонов");
  const next = structuredClone(plan), keys = new Set(selected.map(pageKey));
  const order = new Map(physical.map((p, i) => [pageKey(p), i]));
  next.excluded_pages = next.excluded_pages.filter(p => !keys.has(pageKey(p)));
  next.documents.push({ client_id: newGroupId(), pages: [...selected].sort((a, b) => order.get(pageKey(a)) - order.get(pageKey(b))) });
  next.documents.sort((a, b) => order.get(pageKey(a.pages[0])) - order.get(pageKey(b.pages[0])));
  return next;
}

export function moveBoundary(plan, clientId, side, target, physical) {
  /** Двигает общую границу без пустых групп; свободные страницы назначаются только явным жестом. */
  const next = structuredClone(plan), index = next.documents.findIndex(d => d.client_id === clientId);
  const group = next.documents[index], order = new Map(physical.map((p, i) => [pageKey(p), i]));
  if (!group || target < 0 || target >= physical.length) throw new Error("Граница вне пакета");
  const positions = group.pages.map(p => order.get(pageKey(p)));
  const first = positions[0], last = positions.at(-1);
  if (last - first + 1 !== positions.length) throw new Error("Для несмежных страниц используйте разделение и объединение");
  const adjacent = next.documents[index + (side === "right" ? 1 : -1)];
  const neighborPositions = adjacent?.pages.map(p => order.get(pageKey(p))) || [];
  if (side === "right" && (target < first || (adjacent && target >= neighborPositions.at(-1)))) throw new Error("Нельзя опустошить диапазон: используйте «Объединить»");
  if (side === "left" && (target > last || (adjacent && target <= neighborPositions[0]))) throw new Error("Нельзя опустошить диапазон: используйте «Объединить»");
  const begin = side === "left" ? target : first, end = side === "right" ? target : last;
  const replacement = physical.slice(begin, end + 1), own = new Set(group.pages.map(pageKey));
  const incoming = new Set(replacement.map(pageKey));
  for (const ref of replacement) {
    const owner = groupFor(next, ref);
    if (owner && owner !== group && owner !== adjacent) throw new Error("Нельзя перескочить через другой диапазон");
  }
  const removed = group.pages.filter(p => !incoming.has(pageKey(p)));
  const touches = adjacent && (side === "right" ? neighborPositions[0] === last + 1 : neighborPositions.at(-1) === first - 1);
  if (adjacent) {
    adjacent.pages = adjacent.pages.filter(p => !incoming.has(pageKey(p)));
    if (touches) adjacent.pages.push(...removed);
    adjacent.pages.sort((a, b) => order.get(pageKey(a)) - order.get(pageKey(b)));
    if (!adjacent.pages.length) throw new Error("Для удаления соседнего диапазона используйте «Объединить»");
  }
  next.excluded_pages = next.excluded_pages.filter(p => !incoming.has(pageKey(p)));
  if (!touches) for (const page of removed) next.excluded_pages.push({ ...page });
  group.pages = replacement.map(p => ({ file_id: p.file_id, page_number: p.page_number }));
  if (!own.size) throw new Error("Диапазон не существует");
  return next;
}

export function coversPages(plan, physical) {
  /** Проверяет возможность сохранения на клиенте; не заменяет owner/revision серверную проверку. */
  if (!plan) return false;
  const assigned = [...plan.documents.flatMap(d => d.pages), ...plan.excluded_pages].map(pageKey);
  return assigned.length === physical.length && new Set(assigned).size === assigned.length && physical.every(p => assigned.includes(pageKey(p)));
}
