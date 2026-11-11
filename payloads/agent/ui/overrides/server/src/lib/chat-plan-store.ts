/**
 * Agent Bricks: each chat's agent plan (`spec.runtime.context.plan`) in the
 * chat-history Lakebase.
 *
 * The table lives in upstream's `ai_chatbot` schema and is created on first
 * use, like `chat_reasoning_replay` (see `reasoning-replay-store.ts`).  One
 * row per chat; its foreign key cascades, so deleting a chat (the delete
 * route or the retention sweep) removes its plan.  Callers check that the
 * chat is the user's.  Without Lakebase there is no store.
 */
import { getDb, getSchemaName, isDatabaseAvailable } from '@chat-template/db';
import { DEFAULT_PLAN_MAX_CHARS, type StoredPlan } from './chat-plan-body';
import { envFlag, positiveIntFromEnv } from './env-flag';
import type { Sql } from './sql-transaction';

const TABLE = 'chat_plans';
const NO_PLAN: StoredPlan = { content: '', history: '' };

/** Whether the agent plan is on (`ENABLE_CHAT_PLAN`). */
export function chatPlanEnabled(): boolean {
  return envFlag('ENABLE_CHAT_PLAN');
}

/** The deploy's `plan.max_chars` (`PLAN_MAX_CHARS`). */
export function planMaxChars(): number {
  return positiveIntFromEnv('PLAN_MAX_CHARS', DEFAULT_PLAN_MAX_CHARS);
}

export class ChatPlanStore {
  private readonly schema = getSchemaName();
  private ready: Promise<void> | null = null;

  /** The chat's plan; empty when it has none. */
  async get(chatId: string): Promise<StoredPlan> {
    const sql = await this.client();
    const rows = await sql<StoredPlan[]>`
      SELECT content, history FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE chat_id = ${chatId}`;
    return rows[0] ?? NO_PLAN;
  }

  /** Stores the chat's plan and its history together. */
  async put(chatId: string, plan: StoredPlan): Promise<void> {
    const sql = await this.client();
    await sql`
      INSERT INTO ${sql(this.schema)}.${sql(TABLE)} (chat_id, content, history)
      VALUES (${chatId}, ${plan.content}, ${plan.history})
      ON CONFLICT (chat_id) DO UPDATE
      SET content = EXCLUDED.content, history = EXCLUDED.history, updated_at = now()`;
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
        chat_id uuid PRIMARY KEY REFERENCES ${schema}."Chat"(id) ON DELETE CASCADE,
        content text NOT NULL DEFAULT '',
        history text NOT NULL DEFAULT '',
        updated_at timestamptz NOT NULL DEFAULT now()
      )`;
  }
}

let store: ChatPlanStore | null = null;

/** The store, or `null` when there is no Lakebase. */
export function chatPlanStore(): ChatPlanStore | null {
  if (!isDatabaseAvailable()) {
    return null;
  }
  store ??= new ChatPlanStore();
  return store;
}
