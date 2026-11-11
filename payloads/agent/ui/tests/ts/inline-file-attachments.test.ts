import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import { inlineFileAttachments } from '../../overrides/server/src/lib/inline-file-attachments.ts';

// Messages are built as plain objects and cast through `unknown`, keeping the
// suite runnable without resolving `@chat-template/core` at test time (the
// import above is type-only, erased by Node's `--experimental-strip-types`).

function makeMessage(role: string, parts: unknown[]): ChatMessage {
  return { id: 'm1', role, parts } as unknown as ChatMessage;
}

function partsOf(message: ChatMessage): any[] {
  return (message as unknown as { parts: any[] }).parts;
}

describe('inlineFileAttachments', () => {
  it('swaps an extracted file part for an injected text part', () => {
    const messages = [
      makeMessage('user', [
        { type: 'file', name: 'spec.pdf', mediaType: 'application/pdf', extractedText: 'hello world' },
        { type: 'text', text: 'summarise this' },
      ]),
    ];

    const [out] = inlineFileAttachments(messages);
    const parts = partsOf(out);

    assert.equal(parts.length, 2);
    assert.equal(parts[0].type, 'text');
    assert.match(parts[0].text, /\[Attached file: spec\.pdf\]/);
    assert.match(parts[0].text, /hello world/);
    // The trailing user text is preserved untouched.
    assert.deepEqual(parts[1], { type: 'text', text: 'summarise this' });
  });

  it('marks truncated attachments in the label', () => {
    const messages = [
      makeMessage('user', [
        { type: 'file', name: 'big.csv', mediaType: 'text/csv', extractedText: 'a,b', truncated: true },
      ]),
    ];

    const parts = partsOf(inlineFileAttachments(messages)[0]);
    assert.match(parts[0].text, /\(truncated\)/);
  });

  it('leaves image file parts (no extractedText) untouched', () => {
    const imagePart = { type: 'file', name: 'pic.png', mediaType: 'image/png', url: 'https://x/y.png' };
    const messages = [makeMessage('user', [imagePart, { type: 'text', text: 'hi' }])];

    const parts = partsOf(inlineFileAttachments(messages)[0]);
    assert.deepEqual(parts[0], imagePart);
    assert.equal(parts[1].text, 'hi');
  });

  it('returns the same message reference when nothing changes', () => {
    const messages = [makeMessage('assistant', [{ type: 'text', text: 'ok' }])];
    const out = inlineFileAttachments(messages);
    assert.equal(out[0], messages[0]);
  });

  it('tolerates messages without a parts array', () => {
    const weird = { id: 'm', role: 'user' } as unknown as ChatMessage;
    const out = inlineFileAttachments([weird]);
    assert.equal(out[0], weird);
  });
});
