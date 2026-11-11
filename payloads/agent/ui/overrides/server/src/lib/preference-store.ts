/**
 * Agent Bricks: sidebar picks persisted in the chat-history Lakebase.
 *
 * The table lives in upstream's `ai_chatbot` schema, which the app's
 * service principal already owns, and is created on first use instead of
 * through a drizzle migration so upstream's migration journal stays
 * untouched.  Without Lakebase the picks fall back to process memory.
 */
import { getDb, getSchemaName, isDatabaseAvailable } from '@chat-template/db';
import { envFlag } from './env-flag';
import {
  MemoryPreferenceStore,
  parseSelectedResources,
  type PreferenceStore,
  type SelectedResource,
  type SkillFqnCheck,
} from './resource-preferences';
import { isUserSkillFqn } from './user-skills';

const TABLE = 'user_resource_prefs';

/** `lakebase:<slug>` skill picks are valid only while user skills are on. */
export function userSkillPickCheck(): SkillFqnCheck | undefined {
  return envFlag('ENABLE_USER_SKILLS') ? isUserSkillFqn : undefined;
}

type Sql = Awaited<ReturnType<typeof getDb>>['$client'];

export class LakebasePreferenceStore implements PreferenceStore {
  private readonly schema = getSchemaName();
  private ready: Promise<void> | null = null;

  async get(userId: string): Promise<SelectedResource[]> {
    const sql = await this.client();
    const rows = await sql`
      SELECT resources FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE user_id = ${userId}`;
    if (rows.length === 0) {
      return [];
    }
    // Stored rows are re-validated: the rules may have tightened since.
    const parsed = parseSelectedResources(rows[0].resources, userSkillPickCheck());
    return parsed.ok ? parsed.resources : [];
  }

  async put(userId: string, resources: SelectedResource[]): Promise<void> {
    const sql = await this.client();
    await sql`
      INSERT INTO ${sql(this.schema)}.${sql(TABLE)} (user_id, resources, updated_at)
      VALUES (${userId}, ${JSON.stringify(resources)}::jsonb, now())
      ON CONFLICT (user_id)
      DO UPDATE SET resources = EXCLUDED.resources, updated_at = now()`;
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
        user_id text PRIMARY KEY,
        resources jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
      )`;
  }
}

let store: PreferenceStore | null = null;

export function preferenceStore(): PreferenceStore {
  store ??= isDatabaseAvailable()
    ? new LakebasePreferenceStore()
    : new MemoryPreferenceStore();
  return store;
}

/**
 * The user's picks for one chat request, or `null` for none.  A failing
 * store must not fail the chat: the agent then runs with its own set only.
 */
export async function picksForRequest(
  userId: string,
): Promise<SelectedResource[] | null> {
  if (!envFlag('ENABLE_RESOURCES_SIDEBAR')) {
    return null;
  }
  try {
    const picks = await preferenceStore().get(userId);
    return picks.length > 0 ? picks : null;
  } catch (error) {
    console.error('[preferences] read failed; chatting without picks:', error);
    return null;
  }
}
