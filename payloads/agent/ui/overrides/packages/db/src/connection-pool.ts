/**
 * Override of e2e-chatbot-app-next/packages/db/src/connection-pool.ts.
 *
 * Two changes, both scoped to this file:
 *
 * 1. ``search_path`` is set once, declaratively, as a `postgres.js`
 *    startup parameter (`connection.search_path`) instead of a
 *    ``SET search_path`` query run by every ``getDb()`` caller. Upstream's
 *    per-call ``SET`` is also unsound for a pooled client: the query can
 *    land on any physical connection in the pool, so a later connection
 *    opened lazily (the pool starts empty and grows on demand) would
 *    silently keep the Postgres default ``search_path`` until its own
 *    ``SET`` happened to run. The startup parameter applies to every
 *    physical connection the pool ever opens, known or not yet opened.
 * 2. Routine "obtained a connection" / "set search_path" logs are gated
 *    behind ``DB_POOL_VERBOSE_LOGGING`` (default off). Every Agent Bricks
 *    chat turn calls ``getDb()`` three to four times (chat access,
 *    history, save message, sidebar picks), each of which used to log
 *    4-5 lines through this file and ``queries.ts`` -- real log noise,
 *    not a connection problem. Errors still always log.
 *
 * Everything else (lazy ``postgres`` import, token-change detection,
 * pool sizing/timeouts) is unchanged from upstream.
 */
import { drizzle } from 'drizzle-orm/postgres-js';
import type postgres from 'postgres';
import * as schema from './schema';
import { getConnectionUrl, getSchemaName } from './connection';
import { getDatabricksToken } from '@chat-template/auth';

/** Mirrors server/src/lib/env-flag.ts; duplicated rather than imported so
 *  this package does not depend on the server app. */
function envFlag(name: string): boolean {
  const raw = (process.env[name] ?? '').trim().toLowerCase();
  return raw === 'true' || raw === '1' || raw === 'yes';
}

const VERBOSE = envFlag('DB_POOL_VERBOSE_LOGGING');

// Connection pool management
let sqlConnection: postgres.Sql | null = null;
let currentToken: string | null = null;

async function getConnection(): Promise<postgres.Sql> {
  const { default: postgres } = await import('postgres');
  // Get the current token to check if it's changed
  const freshToken = await getDatabricksToken();

  // If we have a connection but the token has changed, we need to recreate the connection
  // This ensures we're always using a valid token
  if (sqlConnection && currentToken !== freshToken) {
    if (VERBOSE) console.log('[DB Pool] Token changed, closing existing connection pool');
    await sqlConnection.end();
    sqlConnection = null;
    currentToken = null;
  }

  // Create a new connection if needed
  if (!sqlConnection) {
    const connectionUrl = await getConnectionUrl();
    const schemaName = getSchemaName();
    sqlConnection = postgres(connectionUrl, {
      max: 10, // connection pool size
      idle_timeout: 20, // close idle connections after 20 seconds
      connect_timeout: 10,
      // Important: Set max_lifetime to ensure connections don't outlive the token
      // OAuth tokens typically expire in 1 hour, we'll refresh connections more frequently
      max_lifetime: 60 * 10, // 10 minutes max connection lifetime
      // Startup parameter, not a runtime query: applies to every physical
      // connection the pool opens (now or later), so callers never need
      // their own ``SET search_path``.
      ...(schemaName !== 'public' ? { connection: { search_path: `${schemaName}, public` } } : {}),
    });

    currentToken = freshToken;
    if (VERBOSE) console.log('[DB Pool] Created new connection pool with fresh OAuth token');
  }

  return sqlConnection;
}

// Export a function to get the Drizzle instance with fresh connection
export async function getDb() {
  const sql = await getConnection();
  return drizzle(sql, { schema });
}
