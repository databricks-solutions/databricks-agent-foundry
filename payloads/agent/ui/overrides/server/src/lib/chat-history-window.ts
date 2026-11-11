/**
 * Agent Bricks: day-based limits on chat history (#638).
 *
 * `CHAT_HISTORY_SIDEBAR_DAYS` bounds what `GET /api/history` lists and
 * `CHAT_HISTORY_RETENTION_DAYS` what Lakebase keeps; `0` or unset turns
 * either off.  Kept free of runtime imports so
 * `payloads/agent/ui/tests/ts/` can test it.
 */

const DAY_MS = 24 * 60 * 60 * 1000;

export interface HistoryPage<T> {
  chats: T[];
  hasMore: boolean;
}

/** A positive whole day count from `name`, or `undefined` when off or malformed. */
export function daysFromEnv(name: string): number | undefined {
  const days = Number((process.env[name] ?? '').trim());
  return Number.isInteger(days) && days > 0 ? days : undefined;
}

export function cutoffFor(days: number, now: Date): Date {
  return new Date(now.getTime() - days * DAY_MS);
}

/**
 * Drops chats created before `cutoff` from a newest-first page.  Every
 * later page is older still, so a page that lost any chat ends the paging.
 */
export function limitToWindow<T extends { createdAt: Date }>(
  page: HistoryPage<T>,
  cutoff: Date,
): HistoryPage<T> {
  const chats = page.chats.filter((chat) => chat.createdAt >= cutoff);
  return {
    chats,
    hasMore: page.hasMore && chats.length === page.chats.length,
  };
}

/**
 * A sidebar page from a newest-first read of `limit + 1` rows.  The extra
 * row is the next-older chat: there are more chats to list only when it
 * exists and is inside the window, not merely because older chats remain
 * in Lakebase.
 */
export function windowPage<T extends { createdAt: Date }>(
  rows: T[],
  limit: number,
  cutoff: Date,
): HistoryPage<T> {
  const next = rows.at(limit);
  return limitToWindow(
    {
      chats: rows.slice(0, limit),
      hasMore: next !== undefined && next.createdAt >= cutoff,
    },
    cutoff,
  );
}
