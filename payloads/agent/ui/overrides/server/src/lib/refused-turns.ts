import type { ChatMessage } from '@chat-template/core';

/**
 * Agent Bricks: leave turns the model provider refused out of the model
 * request.
 *
 * When the provider refuses a turn (`finish_reason: content_filter`), the
 * agent runtime answers with this notice instead of an empty message.  Sent
 * back on later turns, the refused prompt is usually refused again, so every
 * later turn in the chat fails too.  The chat keeps showing the turn; only
 * the model request drops the notice and the user message before it.
 *
 * Must match `CONTENT_FILTER_NOTICE` in `payloads/agent/runtime/content_filter.py`.
 */
export const CONTENT_FILTER_NOTICE =
  "I couldn't answer that: the model provider declined the request. Please rephrase it and try again.";

type Part = ChatMessage['parts'][number];

/** An assistant message holding only the notice: no other text, no tools, no reasoning. */
function isRefusal(message: ChatMessage): boolean {
  if (message.role !== 'assistant') {
    return false;
  }
  const parts = message.parts.filter((part: Part) => part.type !== 'step-start');
  if (parts.length !== 1) {
    return false;
  }
  const [only] = parts;
  return only.type === 'text' && only.text.trim() === CONTENT_FILTER_NOTICE;
}

/**
 * *messages* without each refusal notice and the user message it answered.
 * The user message *keepMessageId* stays, since the compaction summary is
 * attached to it; its refusal is then kept too, so user and assistant
 * messages still alternate.
 */
export function withoutRefusedTurns(messages: ChatMessage[], keepMessageId?: string): ChatMessage[] {
  const dropped = new Set<number>();
  messages.forEach((message, index) => {
    const asked = messages[index - 1];
    if (!isRefusal(message) || asked?.role !== 'user' || asked.id === keepMessageId) {
      return;
    }
    dropped.add(index - 1);
    dropped.add(index);
  });
  return dropped.size === 0 ? messages : messages.filter((_, index) => !dropped.has(index));
}
