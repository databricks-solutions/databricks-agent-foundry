import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  decodeChatSocketFrame,
  decodeChatSocketRequest,
  encodeChatSocketMessage,
} from '../../overrides/packages/core/src/chat-socket-protocol.ts';

describe('decodeChatSocketRequest', () => {
  it('round-trips a send frame', () => {
    const body = { id: 'chat-1', selectedChatModel: 'chat-model' };
    const data = encodeChatSocketMessage({ type: 'send', body });
    assert.deepEqual(decodeChatSocketRequest(data), { type: 'send', body });
  });

  it('round-trips a resume frame with and without a cursor', () => {
    assert.deepEqual(
      decodeChatSocketRequest(encodeChatSocketMessage({ type: 'resume', chatId: 'chat-1', cursor: 3 })),
      { type: 'resume', chatId: 'chat-1', cursor: 3 },
    );
    assert.deepEqual(
      decodeChatSocketRequest(encodeChatSocketMessage({ type: 'resume', chatId: 'chat-1' })),
      { type: 'resume', chatId: 'chat-1', cursor: undefined },
    );
  });

  it('rejects malformed frames', () => {
    for (const data of [
      'not json',
      '[]',
      'null',
      '{"type":"send"}',
      '{"type":"send","body":"text"}',
      '{"type":"resume"}',
      '{"type":"resume","chatId":"c","cursor":-1}',
      '{"type":"resume","chatId":"c","cursor":1.5}',
      '{"type":"start","streamId":"s"}',
    ]) {
      assert.equal(decodeChatSocketRequest(data), null, data);
    }
  });
});

describe('decodeChatSocketFrame', () => {
  it('round-trips every server frame', () => {
    const frames = [
      { type: 'start', streamId: 'stream-1' },
      { type: 'chunk', index: 0, chunk: { type: 'text-delta', id: 't', delta: 'Hi' } },
      { type: 'end' },
      { type: 'error', code: 'forbidden:chat', cause: 'not_owner' },
    ] as const;
    for (const frame of frames) {
      assert.deepEqual(decodeChatSocketFrame(encodeChatSocketMessage(frame)), frame);
    }
  });

  it('drops an unknown cause type and keeps the code', () => {
    assert.deepEqual(decodeChatSocketFrame('{"type":"error","code":"offline:chat","cause":7}'), {
      type: 'error',
      code: 'offline:chat',
      cause: undefined,
    });
  });

  it('rejects malformed frames', () => {
    for (const data of [
      '{"type":"start"}',
      '{"type":"chunk","index":-1,"chunk":{}}',
      '{"type":"chunk","index":0}',
      '{"type":"error","code":"Bad Code"}',
      '{"type":"send","body":{}}',
      '{"type":"other"}',
    ]) {
      assert.equal(decodeChatSocketFrame(data), null, data);
    }
  });
});
