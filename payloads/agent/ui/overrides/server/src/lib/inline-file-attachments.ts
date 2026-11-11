/**
 * Inline extracted-file attachments into model-visible text (D7/D8).
 *
 * The file-upload capability uploads a document, the extractor returns its
 * text synchronously, and the client persists that text on the user message
 * as a `file` part carrying `extractedText` (see the `filePartSchema`
 * override). The model itself cannot read a document `file` part, so right
 * before `convertToModelMessages` we swap every such part for a plain `text`
 * part containing the extracted content.
 *
 * The swap runs over *all* messages each turn (not just the newest), so a
 * file uploaded earlier in the conversation keeps being re-injected. It is a
 * pure transform that returns new message/part objects and never mutates its
 * input — the original `file` part stays on the persisted message so the UI
 * chip survives and re-injection works on later turns.
 *
 * Mirrors the drop-only design of `sanitize-tool-pairs.ts`; unit-tested out
 * of tree at `bricks/app/tests/ts/`.
 */
import type { ChatMessage } from '@chat-template/core';

interface ExtractedFilePart {
  type: 'file';
  name?: string;
  filename?: string;
  extractedText?: string;
  truncated?: boolean;
}

function isExtractedFilePart(part: unknown): part is ExtractedFilePart {
  return (
    typeof part === 'object' &&
    part !== null &&
    (part as { type?: unknown }).type === 'file' &&
    typeof (part as { extractedText?: unknown }).extractedText === 'string'
  );
}

function toTextPart(part: ExtractedFilePart): { type: 'text'; text: string } {
  const label = part.name ?? part.filename ?? 'file';
  const suffix = part.truncated ? ' (truncated)' : '';
  return {
    type: 'text',
    text: `\n\n[Attached file: ${label}${suffix}]\n${part.extractedText ?? ''}\n`,
  };
}

export function inlineFileAttachments(messages: ChatMessage[]): ChatMessage[] {
  return messages.map((message) => {
    const parts = (message as { parts?: unknown }).parts;
    if (!Array.isArray(parts)) {
      return message;
    }
    let changed = false;
    const nextParts = parts.map((part) => {
      if (isExtractedFilePart(part)) {
        changed = true;
        return toTextPart(part);
      }
      return part;
    });
    return changed
      ? ({ ...message, parts: nextParts } as ChatMessage)
      : message;
  });
}
