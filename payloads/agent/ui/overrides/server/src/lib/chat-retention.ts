/**
 * Agent Bricks: the Lakebase chat-retention sweep loop (#638).
 *
 * Kept free of runtime imports so `payloads/agent/ui/tests/ts/` can test
 * it; the SQL lives in `chat-retention-store.ts`.
 */

/** Deletes up to `limit` expired chats; resolves to how many it deleted. */
export type DeleteExpiredBatch = (cutoff: Date, limit: number) => Promise<number>;

export interface SweepLimits {
  batchSize: number;
  maxBatches: number;
}

export const SWEEP_LIMITS: SweepLimits = { batchSize: 500, maxBatches: 20 };

/**
 * Deletes in batches until one comes back short, bounded by `maxBatches`
 * so a large backlog is worked off over several sweeps.
 */
export async function sweepExpiredChats(
  deleteBatch: DeleteExpiredBatch,
  cutoff: Date,
  limits: SweepLimits = SWEEP_LIMITS,
): Promise<number> {
  let total = 0;
  for (let batch = 0; batch < limits.maxBatches; batch += 1) {
    const deleted = await deleteBatch(cutoff, limits.batchSize);
    total += deleted;
    if (deleted < limits.batchSize) {
      break;
    }
  }
  return total;
}
