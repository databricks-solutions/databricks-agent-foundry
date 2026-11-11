import type { getDb } from '@chat-template/db';

export type Sql = Awaited<ReturnType<typeof getDb>>['$client'];
type TransactionSql = Parameters<Parameters<Sql['begin']>[1]>[0];

/**
 * Agent Bricks: *tx* as a query function.  postgres.js 3.4.8, pinned by
 * upstream's lockfile, types `TransactionSql` without its call signature
 * (restored in 3.4.9); at runtime it is callable like `sql`.
 */
export function transactionQuery(tx: TransactionSql): Sql {
  return tx as unknown as Sql;
}
