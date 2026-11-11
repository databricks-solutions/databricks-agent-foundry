/**
 * Agent Bricks: run a conversation compaction (#637).
 *
 * Shared by the background compaction after a turn (`lib/chat-turn.ts`) and
 * the manual "Compact conversation" action (`routes/compaction.ts`).  It
 * summarizes the persisted transcript between the previous boundary and
 * the new one with the title model -- the agent's own LLM endpoint, called
 * as the app -- and stores the result.  Logs carry counts only, never chat
 * content.
 */
import { type LanguageModel, generateText } from 'ai';
import { getMessagesByChatId } from '@chat-template/db';
import { convertToUIMessages, myProvider } from '@chat-template/core';
import {
  type CompactionView,
  buildSummaryPrompt,
  chunkTranscript,
  compactableSpan,
  summarizerInput,
} from './compaction';
import {
  type CompactionStore,
  type CompactionTrigger,
  type StoredCompaction,
  compactionSettings,
  compactionStore,
} from './compaction-store';

const SUMMARY_MODEL = 'title-model';
const MAX_SUMMARY_TOKENS = 4_000;

// One compaction per chat at a time within this instance; the store's
// insert guards across instances.
const inFlight = new Set<string>();

export interface CompactionRequest {
  trigger: CompactionTrigger;
  focus: string | null;
  sourceTokensEst: number | null;
}

export type CompactionOutcome =
  | { status: 'compacted'; compaction: StoredCompaction }
  | { status: 'nothing_to_compact' }
  | { status: 'busy' };

/**
 * The chat's latest compaction for one chat request, or `null` for none.
 * A failing store must not fail the chat: the request then carries the
 * full transcript.
 */
export async function compactionForRequest(chatId: string): Promise<CompactionView | null> {
  const store = compactionStore();
  if (!store) {
    return null;
  }
  try {
    return await store.latestFor(chatId);
  } catch (error) {
    console.error('[compaction] read failed; sending the full transcript:', error);
    return null;
  }
}

export async function compactChat(
  store: CompactionStore,
  chatId: string,
  request: CompactionRequest,
): Promise<CompactionOutcome> {
  if (inFlight.has(chatId)) {
    return { status: 'busy' };
  }
  inFlight.add(chatId);
  try {
    return await summarizeAndStore(store, chatId, request);
  } finally {
    inFlight.delete(chatId);
  }
}

async function summarizeAndStore(
  store: CompactionStore,
  chatId: string,
  request: CompactionRequest,
): Promise<CompactionOutcome> {
  const settings = compactionSettings();
  const messages = convertToUIMessages(await getMessagesByChatId({ id: chatId }));
  const previous = await store.latestFor(chatId);
  const span = compactableSpan(messages, previous, settings.keepRecentTurns);
  if (span === null) {
    return { status: 'nothing_to_compact' };
  }
  const { start, window, cut } = span;

  const chunks = chunkTranscript(window.slice(0, cut));
  if (chunks.length === 0) {
    return { status: 'nothing_to_compact' };
  }

  const model = await myProvider.languageModel(SUMMARY_MODEL);
  const system = buildSummaryPrompt(settings.instructions, request.focus);
  // Each chunk builds on the summary of the ones before it.
  let summary = await summarizeChunk(
    model,
    system,
    summarizerInput(chunks[0], start === null ? null : (previous?.summary ?? null)),
  );
  for (const chunk of chunks.slice(1)) {
    summary = await summarizeChunk(model, system, summarizerInput(chunk, summary));
  }

  const stored = await store.insertIfLatest(
    chatId,
    {
      boundaryMessageId: window[cut].id,
      summary,
      model: model.modelId,
      trigger: request.trigger,
      sourceTokensEst: request.sourceTokensEst,
    },
    previous?.id ?? null,
  );
  return stored ? { status: 'compacted', compaction: stored } : { status: 'busy' };
}

async function summarizeChunk(model: LanguageModel, system: string, prompt: string): Promise<string> {
  const { text } = await generateText({
    model,
    system,
    prompt,
    // No `temperature`: Claude 5.x endpoints reject the parameter.
    maxOutputTokens: MAX_SUMMARY_TOKENS,
  });
  const summary = text.trim();
  if (!summary) {
    throw new Error('the summary model returned no text');
  }
  return summary;
}

/** Starts an automatic compaction without awaiting it; never rejects. */
export function compactInBackground(
  store: CompactionStore,
  chatId: string,
  sourceTokensEst: number | null,
): void {
  compactChat(store, chatId, { trigger: 'auto', focus: null, sourceTokensEst })
    .then((outcome) => {
      const chars = outcome.status === 'compacted' ? outcome.compaction.summary.length : 0;
      console.log(
        `[compaction] auto chat_id=${chatId} status=${outcome.status} ` +
          `source_tokens_est=${sourceTokensEst ?? 'unknown'} summary_chars=${chars}`,
      );
    })
    .catch((error: unknown) => {
      const reason = error instanceof Error ? error.message : String(error);
      console.error(`[compaction] auto chat_id=${chatId} failed: ${reason}`);
    });
}
