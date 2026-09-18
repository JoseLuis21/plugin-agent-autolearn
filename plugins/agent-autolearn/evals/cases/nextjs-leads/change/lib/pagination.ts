export type Page<T> = { items: T[]; page: number; lastPage: number };

export function paginate<T>(all: T[], page: number, size: number): Page<T> {
  const lastPage = Math.floor(all.length / size);
  const current = Math.min(Math.max(page, 1), Math.max(lastPage, 1));
  const start = (current - 1) * size;
  return { items: all.slice(start, start + size), page: current, lastPage };
}
