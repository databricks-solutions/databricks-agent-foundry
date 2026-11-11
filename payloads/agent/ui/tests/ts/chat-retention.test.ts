import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  type DeleteExpiredBatch,
  sweepExpiredChats,
} from '../../overrides/server/src/lib/chat-retention.ts';

const CUTOFF = new Date('2025-10-26T12:00:00Z');
const LIMITS = { batchSize: 10, maxBatches: 3 };

/** A fake that deletes from a backlog of `remaining` chats and records its calls. */
function backlog(remaining: number) {
  const calls: Array<{ cutoff: Date; limit: number }> = [];
  const deleteBatch: DeleteExpiredBatch = async (cutoff, limit) => {
    calls.push({ cutoff, limit });
    const deleted = Math.min(limit, remaining);
    remaining -= deleted;
    return deleted;
  };
  return { calls, deleteBatch };
}

describe('sweepExpiredChats', () => {
  it('stops after the first short batch', async () => {
    const { calls, deleteBatch } = backlog(14);
    assert.equal(await sweepExpiredChats(deleteBatch, CUTOFF, LIMITS), 14);
    assert.equal(calls.length, 2);
  });

  it('runs once when nothing is expired', async () => {
    const { calls, deleteBatch } = backlog(0);
    assert.equal(await sweepExpiredChats(deleteBatch, CUTOFF, LIMITS), 0);
    assert.equal(calls.length, 1);
  });

  it('leaves a large backlog for the next sweep', async () => {
    const { calls, deleteBatch } = backlog(100);
    assert.equal(await sweepExpiredChats(deleteBatch, CUTOFF, LIMITS), 30);
    assert.equal(calls.length, LIMITS.maxBatches);
  });

  it('passes the cutoff and batch size to every batch', async () => {
    const { calls, deleteBatch } = backlog(25);
    await sweepExpiredChats(deleteBatch, CUTOFF, LIMITS);
    assert.ok(calls.every((call) => call.cutoff === CUTOFF && call.limit === 10));
  });

  it('propagates a failing batch', async () => {
    const failing: DeleteExpiredBatch = async () => {
      throw new Error('lakebase down');
    };
    await assert.rejects(sweepExpiredChats(failing, CUTOFF, LIMITS), /lakebase down/);
  });
});
