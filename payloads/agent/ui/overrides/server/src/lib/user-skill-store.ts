/**
 * Agent Bricks: user skills in the chat-history Lakebase (#642).
 *
 * The table lives in upstream's `ai_chatbot` schema and is created on first
 * use, like `user_resource_prefs` (see `preference-store.ts`).  Every query
 * is scoped to the session's user id, so a user can only reach their own
 * skills.  Skills are kept until the user deletes them; chat retention does
 * not touch them.
 */
import { getDb, getSchemaName, isDatabaseAvailable } from '@chat-template/db';
import { positiveIntFromEnv } from './env-flag';
import { type Sql, transactionQuery } from './sql-transaction';
import type {
  SkillDraft,
  SkillEdit,
  StoredSkill,
  UserSkillLimits,
} from './user-skills';

const TABLE = 'user_skills';
const UNIQUE_VIOLATION = '23505';

/** The deploy's `skills.user_skills` limits, as set on the app. */
export function userSkillLimits(): UserSkillLimits {
  return {
    maxSkills: positiveIntFromEnv('USER_SKILLS_MAX', 20),
    maxBodyChars: positiveIntFromEnv('USER_SKILLS_MAX_MD_CHARS', 20_000),
  };
}


export class SkillLimitError extends Error {}
export class SkillExistsError extends Error {}

interface SkillRow {
  slug: string;
  description: string;
  body: string;
  updated_at: Date;
}

function toStoredSkill(row: SkillRow): StoredSkill {
  return {
    slug: row.slug,
    description: row.description,
    body: row.body,
    updatedAt: row.updated_at,
  };
}

function isUniqueViolation(error: unknown): boolean {
  return (error as { code?: string } | null)?.code === UNIQUE_VIOLATION;
}

export class UserSkillStore {
  private readonly schema = getSchemaName();
  private ready: Promise<void> | null = null;

  async list(userId: string): Promise<StoredSkill[]> {
    const sql = await this.client();
    const rows = await sql<SkillRow[]>`
      SELECT slug, description, body, updated_at
      FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE user_id = ${userId}
      ORDER BY slug`;
    return rows.map(toStoredSkill);
  }

  async getMany(userId: string, slugs: string[]): Promise<StoredSkill[]> {
    if (slugs.length === 0) {
      return [];
    }
    const sql = await this.client();
    const rows = await sql<SkillRow[]>`
      SELECT slug, description, body, updated_at
      FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE user_id = ${userId} AND slug IN ${sql(slugs)}
      ORDER BY slug`;
    return rows.map(toStoredSkill);
  }

  /** Raises {@link SkillLimitError} at `maxSkills`, {@link SkillExistsError} on a taken slug. */
  async create(userId: string, draft: SkillDraft, maxSkills: number): Promise<void> {
    const sql = await this.client();
    try {
      await sql.begin(async (transaction) => {
        const tx = transactionQuery(transaction);
        const [{ count }] = await tx<{ count: number }[]>`
          SELECT count(*)::int AS count
          FROM ${tx(this.schema)}.${tx(TABLE)}
          WHERE user_id = ${userId}`;
        if (count >= maxSkills) {
          throw new SkillLimitError(`You already have ${maxSkills} skills, the maximum.`);
        }
        await tx`
          INSERT INTO ${tx(this.schema)}.${tx(TABLE)} (user_id, slug, description, body)
          VALUES (${userId}, ${draft.slug}, ${draft.description}, ${draft.body})`;
      });
    } catch (error) {
      if (isUniqueViolation(error)) {
        throw new SkillExistsError(`You already have a skill named ${draft.slug}.`);
      }
      throw error;
    }
  }

  /** `false` when the user has no skill with this slug. */
  async update(userId: string, slug: string, edit: SkillEdit): Promise<boolean> {
    const sql = await this.client();
    const rows = await sql`
      UPDATE ${sql(this.schema)}.${sql(TABLE)}
      SET description = ${edit.description}, body = ${edit.body}, updated_at = now()
      WHERE user_id = ${userId} AND slug = ${slug}
      RETURNING slug`;
    return rows.length > 0;
  }

  /** Updates the skill with this slug, or creates it within `maxSkills` (see {@link create}). */
  async save(
    userId: string,
    draft: SkillDraft,
    maxSkills: number,
  ): Promise<'created' | 'updated'> {
    if (await this.update(userId, draft.slug, draft)) {
      return 'updated';
    }
    await this.create(userId, draft, maxSkills);
    return 'created';
  }

  /** `false` when the user has no skill with this slug. */
  async delete(userId: string, slug: string): Promise<boolean> {
    const sql = await this.client();
    const rows = await sql`
      DELETE FROM ${sql(this.schema)}.${sql(TABLE)}
      WHERE user_id = ${userId} AND slug = ${slug}
      RETURNING slug`;
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
        slug text NOT NULL,
        description text NOT NULL,
        body text NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (user_id, slug)
      )`;
  }
}

let store: UserSkillStore | null = null;

/** The store, or `null` without Lakebase (user skills need it). */
export function userSkillStore(): UserSkillStore | null {
  if (!isDatabaseAvailable()) {
    return null;
  }
  store ??= new UserSkillStore();
  return store;
}
