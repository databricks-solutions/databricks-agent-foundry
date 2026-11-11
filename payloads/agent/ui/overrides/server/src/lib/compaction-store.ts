/**
 * Agent Bricks: conversation compaction summaries in the chat-history
 * Lakebase (#637).
 *
 * The table lives in upstream's `ai_chatbot` schema and is created on first
 * use, like `user_resource_prefs` (see `preference-store.ts`).  Both foreign
 * keys cascade: deleting a chat (the delete route or the retention sweep)
 * removes its summaries, and deleting the first message a summary kept
 * verbatim (editing an earlier message) removes the now-stale summary.
 * Without Lakebase there is no store: an unpersisted summary would be
 * regenerated every turn and break the prompt cache every turn.
 */
import { getDb, getSchemaName, isDatabaseAvailable } from '@chat-template/db';
import type { CompactionView } from './compaction';
import { envFlag, positiveIntFromEnv } from './env-flag';
import { type Sql, transactionQuery } from './sql-transaction';

const TABLE = 'chat_compactions';
// Arbitrary but fixed: identifies compaction among the app's advisory locks.
const INSERT_LOCK_KEY = 637_001;

export type CompactionTrigger = 'auto' | 'manual';

export interface StoredCompaction extends CompactionView {
  id: string;
  trigger: CompactionTrigger;
  createdAt: Date;
}

export interface CompactionDraft {
  boundaryMessageId: string;
  summary: string;
  model: string;
  trigger: CompactionTrigger;
  sourceTokensEst: number | null;
}

export interface CompactionSettings {
  triggerTokens: number;
  keepRecentTurns: number;
  instructions: string;
}

/** The deploy's `context.compaction` settings, as set on the app. */
export function compactionSettings(): CompactionSettings {
  return {
    triggerTokens: positiveIntFromEnv('COMPACTION_TRIGGER_TOKENS', 120_000),
    keepRecentTurns: positiveIntFromEnv('COMPACTION_KEEP_TURNS', 3),
    instructions: (process.env.COMPACTION_INSTRUCTIONS ?? '').trim(),
  };
}

interface CompactionRow {
  id: string;
  boundary_message_id: string;
  summary: string;
  trigger: CompactionTrigger;
  created_at: Date | string;
}

function toStoredCompaction(row: CompactionRow): StoredCompaction {
  return {
    id: row.id,
    boundaryMessageId: row.boundary_message_id,
    summary: row.summary,
    trigger: row.trigger,
    // drizzle swaps the client's timestamp parsers for a pass-through.
    createdAt: new Date(row.created_at),
  };
}

export class CompactionStore {
  private readonly schema = getSchemaName();
  private ready: Promise<void> | null = null;

  async latestFor(chatId: string): Promise<StoredCompaction | null> {
    const sql = await this.client();
    const rows = await sql<CompactionRow[]>`
      SELECT id, boundary_message_id, summary, trigger, created_at
      FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE chat_id = ${chatId}
      ORDER BY created_at DESC, id DESC
      LIMIT 1`;
    return rows.length > 0 ? toStoredCompaction(rows[0]) : null;
  }

  async listFor(chatId: string): Promise<StoredCompaction[]> {
    const sql = await this.client();
    const rows = await sql<CompactionRow[]>`
      SELECT id, boundary_message_id, summary, trigger, created_at
      FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE chat_id = ${chatId}
      ORDER BY created_at, id`;
    return rows.map(toStoredCompaction);
  }

  /**
   * Inserts *draft* only while *basedOnId* is still the chat's latest
   * compaction (`null`: none yet), so two app instances compacting the same
   * chat cannot stack summaries.  `null` when another compaction won.
   */
  async insertIfLatest(
    chatId: string,
    draft: CompactionDraft,
    basedOnId: string | null,
  ): Promise<StoredCompaction | null> {
    const sql = await this.client();
    return sql.begin(async (transaction) => {
      const tx = transactionQuery(transaction);
      const [{ locked }] = await tx`
        SELECT pg_try_advisory_xact_lock(${INSERT_LOCK_KEY}::int, hashtext(${chatId})) AS locked`;
      if (!locked) {
        return null;
      }
      const latest = await tx<{ id: string }[]>`
        SELECT id FROM ${tx(this.schema)}.${tx(TABLE)}
        WHERE chat_id = ${chatId}
        ORDER BY created_at DESC, id DESC
        LIMIT 1`;
      if ((latest[0]?.id ?? null) !== basedOnId) {
        return null;
      }
      const [row] = await tx<CompactionRow[]>`
        INSERT INTO ${tx(this.schema)}.${tx(TABLE)}
          (chat_id, boundary_message_id, summary, model, trigger, source_tokens_est)
        VALUES (${chatId}, ${draft.boundaryMessageId}, ${draft.summary}, ${draft.model},
          ${draft.trigger}, ${draft.sourceTokensEst})
        RETURNING id, boundary_message_id, summary, trigger, created_at`;
      return toStoredCompaction(row);
    });
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
        boundary_message_id uuid NOT NULL
          REFERENCES ${schema}."Message"(id) ON DELETE CASCADE,
        summary text NOT NULL,
        model text NOT NULL,
        trigger text NOT NULL CHECK (trigger IN ('auto', 'manual')),
        source_tokens_est integer,
        created_at timestamptz NOT NULL DEFAULT now()
      )`;
    await sql`
      CREATE INDEX IF NOT EXISTS chat_compactions_chat_idx
      ON ${schema}.${sql(TABLE)} (chat_id, created_at)`;
  }
}

let store: CompactionStore | null = null;

/** The store, or `null` when compaction is off or there is no Lakebase. */
export function compactionStore(): CompactionStore | null {
  if (!envFlag('ENABLE_COMPACTION') || !isDatabaseAvailable()) {
    return null;
  }
  store ??= new CompactionStore();
  return store;
}
