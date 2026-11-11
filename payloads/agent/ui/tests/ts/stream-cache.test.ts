import { strict as assert } from 'node:assert';
import { afterEach, beforeEach, describe, it, mock } from 'node:test';
import { StreamCache } from '../../overrides/packages/core/src/stream-cache.ts';

const CHAT_ID = 'chat-1';
const CLEANUP_INTERVAL_MS = 60 * 1000;
const TTL_MS = 5 * 60 * 1000;

function openStream(): ReadableStream<string> {
  return new ReadableStream<string>();
}

function store(cache: StreamCache<string>, streamId: string): void {
  cache.storeStream({ streamId, chatId: CHAT_ID, stream: openStream() });
}

/** A source the test feeds chunk by chunk, and ends explicitly. */
function controlledSource(): {
  stream: ReadableStream<string>;
  push: (chunk: string) => void;
  end: () => void;
} {
  let controller!: ReadableStreamDefaultController<string>;
  const stream = new ReadableStream<string>({
    start(c) {
      controller = c;
    },
  });
  return {
    stream,
    push: (chunk) => controller.enqueue(chunk),
    end: () => controller.close(),
  };
}

async function readAll(stream: ReadableStream<string>): Promise<string[]> {
  const chunks: string[] = [];
  for await (const chunk of stream) chunks.push(chunk);
  return chunks;
}

function tick(): Promise<void> {
  return new Promise((resolve) => setImmediate(resolve));
}

describe('StreamCache replay', () => {
  beforeEach(() => {
    mock.timers.enable({ apis: ['setInterval'] });
  });

  afterEach(() => {
    mock.timers.reset();
  });

  it('replays cached chunks to a late reader, then follows the source', async () => {
    const cache = new StreamCache<string>();
    const source = controlledSource();
    cache.storeStream({ streamId: 'turn-1', chatId: CHAT_ID, stream: source.stream });
    source.push('a');
    source.push('b');
    await tick();

    const reading = readAll(cache.getStream('turn-1')!);
    source.push('c');
    source.end();

    assert.deepEqual(await reading, ['a', 'b', 'c']);
  });

  it('starts at the cursor, a chunk index', async () => {
    const cache = new StreamCache<string>();
    const source = controlledSource();
    cache.storeStream({ streamId: 'turn-1', chatId: CHAT_ID, stream: source.stream });
    for (const chunk of ['a', 'b', 'c']) source.push(chunk);
    source.end();

    assert.deepEqual(await readAll(cache.getStream('turn-1', { cursor: 2 })!), ['c']);
  });

  it('keeps draining the source when a reader cancels', async () => {
    const cache = new StreamCache<string>();
    const source = controlledSource();
    cache.storeStream({ streamId: 'turn-1', chatId: CHAT_ID, stream: source.stream });
    const first = cache.getStream('turn-1')!.getReader();
    source.push('a');
    assert.deepEqual(await first.read(), { value: 'a', done: false });
    await first.cancel();

    source.push('b');
    source.end();

    assert.deepEqual(await readAll(cache.getStream('turn-1')!), ['a', 'b']);
  });
});

describe('StreamCache', () => {
  beforeEach(() => {
    mock.timers.enable({ apis: ['setInterval', 'Date'] });
  });

  afterEach(() => {
    mock.timers.reset();
  });

  it('clears the active stream without a stream id', () => {
    const cache = new StreamCache<string>();
    store(cache, 'turn-1');

    cache.clearActiveStream(CHAT_ID);

    assert.equal(cache.getActiveStreamId(CHAT_ID), null);
  });

  it('keeps a newer stream when an earlier turn finishes', () => {
    const cache = new StreamCache<string>();
    store(cache, 'turn-1');
    store(cache, 'turn-2');

    cache.clearActiveStream(CHAT_ID, 'turn-1');

    assert.equal(cache.getActiveStreamId(CHAT_ID), 'turn-2');
  });

  it('keeps a newer stream when an earlier one expires', () => {
    const cache = new StreamCache<string>();
    store(cache, 'turn-1');
    mock.timers.tick(TTL_MS - CLEANUP_INTERVAL_MS);
    store(cache, 'turn-2');

    mock.timers.tick(2 * CLEANUP_INTERVAL_MS);

    assert.equal(cache.getStream('turn-1'), null);
    assert.equal(cache.getActiveStreamId(CHAT_ID), 'turn-2');
  });

  it('drops the active stream when it expires', () => {
    const cache = new StreamCache<string>();
    store(cache, 'turn-1');

    mock.timers.tick(TTL_MS + CLEANUP_INTERVAL_MS);

    assert.equal(cache.getActiveStreamId(CHAT_ID), null);
  });
});
