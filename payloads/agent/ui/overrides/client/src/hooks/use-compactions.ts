import useSWR from 'swr';
import { useAppConfig } from '@/contexts/AppConfigContext';

/** One stored summary, as `GET /api/chat/:id/compactions` returns it. */
export interface Compaction {
  id: string;
  boundaryMessageId: string;
  summary: string;
  trigger: 'auto' | 'manual';
  createdAt: string;
}

interface CompactionsResponse {
  compactions: Compaction[];
}

export type CompactOutcome = 'compacted' | 'nothing_to_compact' | 'busy';

export function compactionsKey(chatId: string): string {
  return `/api/chat/${encodeURIComponent(chatId)}/compactions`;
}

async function getCompactions(url: string): Promise<CompactionsResponse> {
  const response = await fetch(url, { credentials: 'same-origin' });
  if (!response.ok) {
    throw new Error(`Request failed (${response.status})`);
  }
  return response.json() as Promise<CompactionsResponse>;
}

const POLL_INTERVAL_MS = 3_000;

/**
 * The chat's summaries keyed by the first message each one kept verbatim.
 * With `pollUntilMs` (epoch ms), refetches every few seconds until then.
 */
export function useCompactions(
  chatId: string,
  pollUntilMs: number | null = null,
): ReadonlyMap<string, Compaction> {
  const { compactionEnabled } = useAppConfig();
  const { data } = useSWR<CompactionsResponse>(
    compactionEnabled ? compactionsKey(chatId) : null,
    getCompactions,
    {
      revalidateOnFocus: false,
      refreshInterval: () =>
        pollUntilMs !== null && Date.now() < pollUntilMs ? POLL_INTERVAL_MS : 0,
    },
  );
  return new Map((data?.compactions ?? []).map((c) => [c.boundaryMessageId, c]));
}

/** Compacts the chat now; throws with the server's reason. */
export async function compactConversation(
  chatId: string,
  focus: string,
): Promise<CompactOutcome> {
  const response = await fetch(`/api/chat/${encodeURIComponent(chatId)}/compact`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ focus: focus.trim() || null }),
  });
  const body = (await response.json().catch(() => null)) as {
    status?: CompactOutcome;
    error?: string;
  } | null;
  if (body?.status) {
    return body.status;
  }
  throw new Error(body?.error ?? `Request failed (${response.status})`);
}
