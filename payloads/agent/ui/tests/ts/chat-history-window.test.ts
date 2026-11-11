import { strict as assert } from 'node:assert';
import { afterEach, describe, it } from 'node:test';
import {
  cutoffFor,
  daysFromEnv,
  limitToWindow,
  windowPage,
} from '../../overrides/server/src/lib/chat-history-window.ts';

const NOW = new Date('2026-10-26T12:00:00Z');
const daysAgo = (days: number) => ({ createdAt: cutoffFor(days, NOW) });

describe('daysFromEnv', () => {
  const name = 'CHAT_HISTORY_TEST_DAYS';
  afterEach(() => {
    delete process.env[name];
  });

  it('reads a positive whole number', () => {
    process.env[name] = ' 30 ';
    assert.equal(daysFromEnv(name), 30);
  });

  it('treats unset, zero and malformed values as off', () => {
    for (const raw of [undefined, '', '0', '-3', '1.5', 'thirty']) {
      if (raw === undefined) {
        delete process.env[name];
      } else {
        process.env[name] = raw;
      }
      assert.equal(daysFromEnv(name), undefined, String(raw));
    }
  });
});

describe('cutoffFor', () => {
  it('subtracts whole days', () => {
    assert.equal(cutoffFor(30, NOW).toISOString(), '2026-09-26T12:00:00.000Z');
  });
});

describe('limitToWindow', () => {
  const cutoff = cutoffFor(30, NOW);

  it('keeps a page inside the window untouched', () => {
    const page = { chats: [daysAgo(1), daysAgo(29)], hasMore: true };
    assert.deepEqual(limitToWindow(page, cutoff), page);
  });

  it('drops older chats and ends the paging at the boundary', () => {
    const recent = daysAgo(2);
    const page = { chats: [recent, daysAgo(31), daysAgo(40)], hasMore: true };
    assert.deepEqual(limitToWindow(page, cutoff), {
      chats: [recent],
      hasMore: false,
    });
  });

  it('keeps a chat created exactly at the cutoff', () => {
    const page = { chats: [daysAgo(30)], hasMore: false };
    assert.equal(limitToWindow(page, cutoff).chats.length, 1);
  });

  it('never turns hasMore on', () => {
    const page = { chats: [daysAgo(1)], hasMore: false };
    assert.equal(limitToWindow(page, cutoff).hasMore, false);
  });
});

describe('windowPage', () => {
  const cutoff = cutoffFor(30, NOW);

  it('ends the paging when the next older chat is outside the window', () => {
    const page = [daysAgo(1), daysAgo(29)];
    assert.deepEqual(windowPage([...page, daysAgo(31)], 2, cutoff), {
      chats: page,
      hasMore: false,
    });
  });

  it('ends the paging when there is no next older chat', () => {
    assert.equal(windowPage([daysAgo(1), daysAgo(2)], 2, cutoff).hasMore, false);
  });

  it('keeps paging when the next older chat is inside the window', () => {
    const page = [daysAgo(1), daysAgo(2)];
    assert.deepEqual(windowPage([...page, daysAgo(5)], 2, cutoff), {
      chats: page,
      hasMore: true,
    });
  });

  it('drops chats outside the window from the page itself', () => {
    const recent = daysAgo(2);
    assert.deepEqual(windowPage([recent, daysAgo(31), daysAgo(40)], 2, cutoff), {
      chats: [recent],
      hasMore: false,
    });
  });
});
