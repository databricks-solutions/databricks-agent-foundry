/**
 * Agent Bricks: the agent's reasoning replay entries (#681) in the
 * chat-history Lakebase.
 *
 * The table lives in upstream's `ai_chatbot` schema and is created on first
 * use, like `chat_compactions` (see `compaction-store.ts`).  Both foreign
 * keys cascade: deleting a chat (the delete route or the retention sweep)
 * or the assistant message an entry came with (editing an earlier message)
 * removes the entries.  Without Lakebase there is no store, and the agent
 * is not asked for entries.
 */
import { getDb, getSchemaName, isDatabaseAvailable } from '@chat-template/db';
import { isReplayEntry, type ReplayEntry } from './reasoning-replay';
import type { Sql } from './sql-transaction';

const TABLE = 'chat_reasoning_replay';

export class ReasoningReplayStore {
  private readonly schema = getSchemaName();
  private ready: Promise<void> | null = null;

  /** The chat's entries, oldest first; stored rows are re-validated. */
  async listFor(chatId: string): Promise<ReplayEntry[]> {
    const sql = await this.client();
    const rows = await sql<{ entry: unknown }[]>`
      SELECT entry FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE chat_id = ${chatId}
      ORDER BY created_at, seq`;
    return rows.map((row) => row.entry).filter(isReplayEntry);
  }

  /** Stores *entries* as the ones *messageId*'s run produced, in order. */
  async append(chatId: string, messageId: string, entries: ReplayEntry[]): Promise<void> {
    if (entries.length === 0) {
      return;
    }
    const sql = await this.client();
    await sql`
      INSERT INTO ${sql(this.schema)}.${sql(TABLE)} (chat_id, message_id, seq, entry)
      SELECT ${chatId}, ${messageId}, item.seq, item.entry
      FROM jsonb_array_elements(${JSON.stringify(entries)}::jsonb) WITH ORDINALITY AS item(entry, seq)`;
  }

  private async client(): Promise<Sql> {
    const sql = (await getDb()).$client;
    this.ready ??= this.createTable(sql).catch((error: unknown) => {
      this.ready = null;
      throw error;
    });
    await this.ready;
    return sql;
  }

  private async createTable(sql: Sql): Promise<void> {
    const schema = sql(this.schema);
    await sql`
      CREATE TABLE IF NOT EXISTS ${schema}.${sql(TABLE)} (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        chat_id uuid NOT NULL REFERENCES ${schema}."Chat"(id) ON DELETE CASCADE,
        message_id uuid NOT NULL REFERENCES ${schema}."Message"(id) ON DELETE CASCADE,
        seq bigint NOT NULL,
        entry jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
      )`;
    await sql`
      CREATE INDEX IF NOT EXISTS chat_reasoning_replay_chat_idx
      ON ${schema}.${sql(TABLE)} (chat_id, created_at, seq)`;
  }
}

let store: ReasoningReplayStore | null = null;

/** The store, or `null` when there is no Lakebase. */
export function reasoningReplayStore(): ReasoningReplayStore | null {
  if (!isDatabaseAvailable()) {
    return null;
  }
  store ??= new ReasoningReplayStore();
  return store;
}

/** The chat's stored entries; `[]` (logged) when the read fails, which only costs a cache miss. */
export async function replayEntriesForChat(store: ReasoningReplayStore, chatId: string): Promise<ReplayEntry[]> {
  try {
    return await store.listFor(chatId);
  } catch (error) {
    console.warn(`[reasoning-replay] read failed for chat_id=${chatId}:`, error instanceof Error ? error.message : error);
    return [];
  }
}
