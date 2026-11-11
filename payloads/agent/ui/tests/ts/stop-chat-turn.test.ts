import { strict as assert } from 'node:assert';
import { afterEach, describe, it, mock } from 'node:test';
import { requestTurnStop } from '../../overrides/client/src/lib/stop-chat-turn.ts';

const settle = () => new Promise((resolve) => setImmediate(resolve));

describe('requestTurnStop', () => {
  afterEach(() => mock.restoreAll());

  it('sends DELETE for the named turn', () => {
    const fetchMock = mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }));

    requestTurnStop('chat 1', 'stream-1');

    const [url, init] = fetchMock.mock.calls[0].arguments;
    assert.equal(url, '/api/chat/chat%201/stream?streamId=stream-1');
    assert.deepEqual(init, { method: 'DELETE', credentials: 'include' });
  });

  it('omits the stream id when the transport did not report one', () => {
    const fetchMock = mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }));

    requestTurnStop('chat-1');

    assert.equal(fetchMock.mock.calls[0].arguments[0], '/api/chat/chat-1/stream');
  });

  it('swallows a failed request', async () => {
    mock.method(globalThis, 'fetch', async () => {
      throw new TypeError('network down');
    });
    const warn = mock.method(console, 'warn', () => {});

    requestTurnStop('chat-1');
    await settle();

    assert.equal(warn.mock.callCount(), 1);
  });
});
