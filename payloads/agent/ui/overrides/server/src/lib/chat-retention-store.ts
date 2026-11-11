/**
 * Agent Bricks: delete Lakebase chats past `CHAT_HISTORY_RETENTION_DAYS`.
 *
 * Age is the chat's last message (its creation time when it has none), so
 * a chat still in use is never deleted.  Upstream's foreign keys do not
 * cascade, so votes, messages and the chat go in one statement; Postgres
 * checks `NO ACTION` constraints at its end.  Each batch takes a
 * transaction-scoped advisory lock, so concurrent app instances never
 * sweep at the same time.  Tables that reference the chat with
 * `ON DELETE CASCADE` (compactions, reasoning replay, the agent plan) go
 * with it.  Only Lakebase chat data is touched; MLflow traces and feedback
 * are not.
 */
import { getDb, getSchemaName, isDatabaseAvailable } from '@chat-template/db';
import { cutoffFor, daysFromEnv } from './chat-history-window';
import { type DeleteExpiredBatch, sweepExpiredChats } from './chat-retention';
import { transactionQuery } from './sql-transaction';

const FIRST_SWEEP_DELAY_MS = 60 * 1000;
const SWEEP_INTERVAL_MS = 24 * 60 * 60 * 1000;
// Arbitrary but fixed: identifies this sweep among the app's advisory locks.
const SWEEP_LOCK_KEY = 638_001;

const deleteExpiredBatch: DeleteExpiredBatch = async (cutoff, limit) => {
  const sql = (await getDb()).$client;
  // drizzle swaps the client's timestamp serializers for a pass-through,
  // so a `Date` parameter would fail to bind.
  const before = cutoff.toISOString();
  return sql.begin(async (transaction): Promise<number> => {
    const tx = transactionQuery(transaction);
    const schema = tx(getSchemaName());
    const [{ locked }] = await tx`SELECT pg_try_advisory_xact_lock(${SWEEP_LOCK_KEY}) AS locked`;
    if (!locked) {
      return 0;
    }
    const deleted = await tx`
      WITH last_message AS (
        SELECT "chatId", max("createdAt") AS at
        FROM ${schema}."Message"
        GROUP BY "chatId"
      ), expired AS (
        SELECT c.id
        FROM ${schema}."Chat" c
        LEFT JOIN last_message m ON m."chatId" = c.id
        WHERE c."createdAt" < ${before}
          AND coalesce(m.at, c."createdAt") < ${before}
        LIMIT ${limit}
      ), votes AS (
        DELETE FROM ${schema}."Vote" v USING expired e WHERE v."chatId" = e.id
      ), messages AS (
        DELETE FROM ${schema}."Message" m USING expired e WHERE m."chatId" = e.id
      )
      DELETE FROM ${schema}."Chat" c USING expired e WHERE c.id = e.id
      RETURNING c.id`;
    return deleted.length;
  });
};

async function runSweep(retentionDays: number): Promise<void> {
  try {
    const cutoff = cutoffFor(retentionDays, new Date());
    const deleted = await sweepExpiredChats(deleteExpiredBatch, cutoff);
    console.log(
      `[chat-retention] deleted ${deleted} chat(s) inactive since before ${cutoff.toISOString()}`,
    );
  } catch (error) {
    console.error('[chat-retention] sweep failed:', error);
  }
}

/** Schedules the sweep when retention is on and Lakebase is bound. */
export function startChatRetention(): void {
  const retentionDays = daysFromEnv('CHAT_HISTORY_RETENTION_DAYS');
  if (!retentionDays || !isDatabaseAvailable()) {
    return;
  }
  const sweep = () => void runSweep(retentionDays);
  setTimeout(sweep, FIRST_SWEEP_DELAY_MS).unref();
  setInterval(sweep, SWEEP_INTERVAL_MS).unref();
}
