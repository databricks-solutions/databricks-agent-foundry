/**
 * Agent Bricks: long-term agent memory in the chat-history Lakebase (#787).
 *
 * One row per fact.  The table lives in upstream's `ai_chatbot` schema and
 * is created on first use, like `user_skills` (see `user-skill-store.ts`).
 * Every query is scoped to the session's user id, so a user only reaches
 * their own memory.  Facts are kept until the agent forgets them; chat
 * deletion and retention do not touch them.  Without Lakebase there is no
 * store.
 */
import { getDb, getSchemaName, isDatabaseAvailable } from '@chat-template/db';
import {
  DEFAULT_MAX_TOPIC_CHARS,
  type MemoryFact,
  type TopicCount,
  factKey,
  topicChars,
} from './agent-memory';
import { envFlag, positiveIntFromEnv } from './env-flag';
import { type Sql, transactionQuery } from './sql-transaction';

const TABLE = 'agent_memory_facts';

/** Whether long-term memory is on (`ENABLE_AGENT_MEMORY`). */
export function agentMemoryEnabled(): boolean {
  return envFlag('ENABLE_AGENT_MEMORY');
}

/** The deploy's `memory.max_entry_chars` (`AGENT_MEMORY_MAX_TOPIC_CHARS`). */
export function maxTopicChars(): number {
  return positiveIntFromEnv('AGENT_MEMORY_MAX_TOPIC_CHARS', DEFAULT_MAX_TOPIC_CHARS);
}

/** Raised when a fact would take its topic past {@link maxTopicChars}; nothing is saved. */
export class TopicFullError extends Error {}

export class AgentMemoryStore {
  private readonly schema = getSchemaName();
  private ready: Promise<void> | null = null;

  /** The user's topics and how many facts each holds, by topic. */
  async topics(userId: string): Promise<TopicCount[]> {
    const sql = await this.client();
    return sql<TopicCount[]>`
      SELECT topic, count(*)::int AS facts
      FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE user_id = ${userId}
      GROUP BY topic
      ORDER BY topic`;
  }

  /** The user's facts, by topic and then oldest first; one topic's with *topic*. */
  async facts(userId: string, topic?: string): Promise<MemoryFact[]> {
    const sql = await this.client();
    return topic === undefined
      ? sql<MemoryFact[]>`
          SELECT topic, fact FROM ${sql(this.schema)}.${sql(TABLE)}
          WHERE user_id = ${userId}
          ORDER BY topic, created_at`
      : sql<MemoryFact[]>`
          SELECT topic, fact FROM ${sql(this.schema)}.${sql(TABLE)}
          WHERE user_id = ${userId} AND topic = ${topic}
          ORDER BY created_at`;
  }

  /** `exists` when the topic already holds this fact; raises {@link TopicFullError} past *maxChars*. */
  async remember(userId: string, entry: MemoryFact, maxChars: number): Promise<'created' | 'exists'> {
    const sql = await this.client();
    return sql.begin(async (transaction): Promise<'created' | 'exists'> => {
      const tx = transactionQuery(transaction);
      const inserted = await tx`
        INSERT INTO ${tx(this.schema)}.${tx(TABLE)} (user_id, topic, fact, fact_key)
        VALUES (${userId}, ${entry.topic}, ${entry.fact}, ${factKey(entry.fact)})
        ON CONFLICT DO NOTHING
        RETURNING fact_key`;
      if (inserted.length === 0) {
        return 'exists';
      }
      const rows = await tx<{ fact: string }[]>`
        SELECT fact FROM ${tx(this.schema)}.${tx(TABLE)}
        WHERE user_id = ${userId} AND topic = ${entry.topic}`;
      if (topicChars(rows.map((row) => row.fact)) > maxChars) {
        throw new TopicFullError(`/${entry.topic}.md is full (${maxChars} characters).`);
      }
      return 'created';
    });
  }

  /** `false` when the topic holds no such fact. */
  async forget(userId: string, entry: MemoryFact): Promise<boolean> {
    const sql = await this.client();
    const rows = await sql`
      DELETE FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE user_id = ${userId} AND topic = ${entry.topic} AND fact_key = ${factKey(entry.fact)}
      RETURNING fact_key`;
    return rows.length > 0;
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
    await sql`
      CREATE TABLE IF NOT EXISTS ${sql(this.schema)}.${sql(TABLE)} (
        user_id text NOT NULL,
        topic text NOT NULL,
        fact text NOT NULL,
        fact_key text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, topic, fact_key)
      )`;
  }
}

let store: AgentMemoryStore | null = null;

/** The store, or `null` without Lakebase (memory needs it). */
export function agentMemoryStore(): AgentMemoryStore | null {
  if (!isDatabaseAvailable()) {
    return null;
  }
  store ??= new AgentMemoryStore();
  return store;
}
