// Idempotency guard for Telegram webhook updates.
// Telegram retries delivery on non-2xx / timeout, which would otherwise re-run
// handlers (duplicate leads, duplicate DeepSeek spend). We remember recently
// seen update_id values in a bounded LRU set.
//
// update_id is per-bot, so each bot gets its own guard (createUpdateDedup);
// the default export-style `isDuplicateUpdate` is the main bot's guard.
//
// NOTE: in-memory only — survives within a single process. For multi-instance
// deployments this should move to Redis/DB (tracked in the roadmap).

const MAX = 2000;

export function createUpdateDedup(): (updateId: unknown) => boolean {
  const seen = new Set<number>();
  const order: number[] = [];
  /** Returns true if this update_id was already processed (i.e. a retry). */
  return (updateId: unknown): boolean => {
    if (typeof updateId !== "number" || !Number.isFinite(updateId)) return false;
    if (seen.has(updateId)) return true;
    seen.add(updateId);
    order.push(updateId);
    if (order.length > MAX) {
      const evicted = order.shift();
      if (evicted !== undefined) seen.delete(evicted);
    }
    return false;
  };
}

export const isDuplicateUpdate = createUpdateDedup();
