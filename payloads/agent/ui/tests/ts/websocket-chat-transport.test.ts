import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, describe, it, mock } from 'node:test';
import { ChatSDKError } from '@chat-template/core/errors';
import { sseCalls, sseResume } from './support/chat-transport.ts';

const TRANSPORT_URL = new URL(
  '../../overrides/client/src/lib/websocket-chat-transport.ts',
  import.meta.url,
).href;

type Frame = Record<string, unknown>;

class FakeWebSocket {
  static instances: FakeWebSocket[] = [];
  readonly url: string;
  readonly sent: Frame[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    queueMicrotask(() => this.onclose?.());
  }

  open(): void {
    this.onopen?.();
  }

  receive(frame: Frame): void {
    this.onmessage?.({ data: JSON.stringify(frame) });
  }

  /** The network drops the socket, as opposed to either side closing it. */
  drop(): void {
    this.closed = true;
    this.onclose?.();
  }
}

let loads = 0;

/** A fresh module per test, so the page-session fallback flag starts clear. */
async function newTransport(parts: unknown[] = [], turnIds: string[] = [], noTurns: string[] = []) {
  const { WebSocketChatTransport } = await import(`${TRANSPORT_URL}?load=${loads++}`);
  return new WebSocketChatTransport({
    onStreamPart: (part: unknown) => parts.push(part),
    onTurnStart: (streamId: string) => turnIds.push(streamId),
    onNoActiveTurn: () => noTurns.push('none'),
    prepareSendMessagesRequest: ({ id }: { id: string }) => ({ body: { id, prepared: true } }),
  });
}

function sendOptions(abortSignal?: AbortSignal) {
  return {
    chatId: 'chat-1',
    messages: [],
    trigger: 'submit-message' as const,
    messageId: undefined,
    abortSignal,
  };
}

async function readAll<T>(stream: ReadableStream<T>): Promise<T[]> {
  const chunks: T[] = [];
  for await (const chunk of stream) chunks.push(chunk);
  return chunks;
}

const latestSocket = (): FakeWebSocket => {
  const socket = FakeWebSocket.instances.at(-1);
  assert.ok(socket, 'no socket opened');
  return socket;
};

const settle = () => new Promise((resolve) => setImmediate(resolve));

type Transport = Awaited<ReturnType<typeof newTransport>>;

/** Starts a send and waits until its socket exists (the body resolves first). */
async function startSend(transport: Transport, abortSignal?: AbortSignal) {
  const pending: Promise<ReadableStream<unknown>> = transport.sendMessages(sendOptions(abortSignal));
  await settle();
  return { pending };
}

const TEXT = { type: 'text-delta', id: 't', delta: 'Hi' };

describe('WebSocketChatTransport', () => {
  beforeEach(() => {
    FakeWebSocket.instances = [];
    sseCalls.length = 0;
    sseResume.empty = false;
    Object.assign(globalThis, {
      WebSocket: FakeWebSocket,
      window: { location: { href: 'https://app.example/chat/chat-1' } },
    });
  });

  afterEach(() => mock.timers.reset());

  it('sends the prepared body over the socket and streams the chunks', async () => {
    const parts: unknown[] = [];
    const turnIds: string[] = [];
    const transport = await newTransport(parts, turnIds);
    const { pending } = await startSend(transport);
    const socket = latestSocket();

    socket.open();
    socket.receive({ type: 'start', streamId: 's1' });
    socket.receive({ type: 'chunk', index: 0, chunk: TEXT });
    socket.receive({ type: 'end' });

    assert.deepEqual(await readAll(await pending), [TEXT]);
    assert.equal(socket.url, 'wss://app.example/api/chat/socket');
    assert.deepEqual(socket.sent, [{ type: 'send', body: { id: 'chat-1', prepared: true } }]);
    assert.deepEqual(parts, [TEXT]);
    assert.deepEqual(turnIds, ['s1']);
    assert.deepEqual(sseCalls, []);
  });

  it('throws the server error code without falling back', async () => {
    const transport = await newTransport();
    const { pending } = await startSend(transport);
    latestSocket().open();
    latestSocket().receive({ type: 'error', code: 'forbidden:chat', cause: 'not_owner' });

    await assert.rejects(pending, (error: unknown) => {
      assert.ok(error instanceof ChatSDKError);
      assert.equal(`${error.type}:${error.surface}`, 'forbidden:chat');
      assert.equal(error.cause, 'not_owner');
      return true;
    });
    assert.deepEqual(sseCalls, []);
  });

  it('falls back to SSE when the socket cannot open, then stays on SSE', async () => {
    const transport = await newTransport();
    const { pending } = await startSend(transport);
    latestSocket().drop();

    assert.deepEqual(await readAll(await pending), [{ type: 'sse' }]);
    await transport.sendMessages(sendOptions());
    assert.deepEqual(sseCalls, ['send', 'send']);
    assert.equal(FakeWebSocket.instances.length, 1);
  });

  it('falls back to SSE when the socket does not open in time', async () => {
    mock.timers.enable({ apis: ['setTimeout'] });
    const transport = await newTransport();
    const { pending } = await startSend(transport);

    mock.timers.tick(4_000);

    await pending;
    assert.deepEqual(sseCalls, ['send']);
  });

  it('does not re-send a turn when the socket drops before start', async () => {
    const transport = await newTransport();
    const { pending } = await startSend(transport);
    latestSocket().open();
    latestSocket().drop();

    await assert.rejects(pending, (error: unknown) => {
      assert.ok(error instanceof ChatSDKError);
      assert.equal(`${error.type}:${error.surface}`, 'offline:chat');
      return true;
    });
    assert.deepEqual(sseCalls, []);
  });

  it('ends the stream without finish on a mid-reply drop and resumes over SSE', async () => {
    const transport = await newTransport();
    const { pending } = await startSend(transport);
    const socket = latestSocket();
    socket.open();
    socket.receive({ type: 'start', streamId: 's1' });
    socket.receive({ type: 'chunk', index: 0, chunk: TEXT });
    socket.drop();

    assert.deepEqual(await readAll(await pending), [TEXT]);
    await transport.reconnectToStream({ chatId: 'chat-1' });
    assert.deepEqual(sseCalls, ['resume']);
  });

  it('resumes over the socket and returns null when nothing is streaming', async () => {
    const noTurns: string[] = [];
    const transport = await newTransport([], [], noTurns);
    const pending = transport.reconnectToStream({ chatId: 'chat-1' });
    latestSocket().open();
    latestSocket().receive({ type: 'error', code: 'empty:stream' });

    assert.equal(await pending, null);
    assert.deepEqual(latestSocket().sent, [{ type: 'resume', chatId: 'chat-1' }]);
    assert.deepEqual(noTurns, ['none']);
  });

  it('resumes over SSE when the socket cannot open', async () => {
    const noTurns: string[] = [];
    const transport = await newTransport([], [], noTurns);
    const pending = transport.reconnectToStream({ chatId: 'chat-1' });
    latestSocket().drop();

    assert.ok(await pending);
    assert.deepEqual(sseCalls, ['resume']);
    assert.deepEqual(noTurns, []);
  });

  it('reports no running turn when the SSE resume finds none', async () => {
    sseResume.empty = true;
    const noTurns: string[] = [];
    const transport = await newTransport([], [], noTurns);
    const pending = transport.reconnectToStream({ chatId: 'chat-1' });
    latestSocket().drop();

    assert.equal(await pending, null);
    assert.deepEqual(noTurns, ['none']);
  });

  it('rejects with an AbortError and closes the socket when aborted', async () => {
    const controller = new AbortController();
    const transport = await newTransport();
    const { pending } = await startSend(transport, controller.signal);
    latestSocket().open();

    controller.abort();

    await assert.rejects(pending, { name: 'AbortError' });
    assert.equal(latestSocket().closed, true);
  });

  it('errors an open stream with an AbortError when aborted mid-reply', async () => {
    const controller = new AbortController();
    const transport = await newTransport();
    const { pending } = await startSend(transport, controller.signal);
    latestSocket().open();
    latestSocket().receive({ type: 'start', streamId: 's1' });
    const stream = await pending;

    controller.abort();
    await settle();

    await assert.rejects(readAll(stream), { name: 'AbortError' });
  });
});
