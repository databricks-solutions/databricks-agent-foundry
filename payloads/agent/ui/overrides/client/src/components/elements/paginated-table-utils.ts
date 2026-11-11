/**
 * Parse / validate the payload embedded in a ``table-download`` fence.
 *
 * Design adapted from
 * https://github.com/databricks-solutions/dbx-unifiedchat
 * at commit e1e1732b1c0a5b14b0208898ebaab71119164498.
 */
import { decodeBase64Utf8 } from './message-context';

export interface TablePayload {
  columns: string[];
  rows: unknown[][];
  totalRows: number;
}

/**
 * Decode + validate a base64-encoded ``{columns,rows,totalRows}``
 * payload.  Returns ``null`` on any structural issue so the caller
 * can fall back to the raw fence.
 */
export function parseTablePayload(payloadB64: string): TablePayload | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(decodeBase64Utf8(payloadB64));
  } catch {
    return null;
  }
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    !Array.isArray((parsed as TablePayload).columns) ||
    !Array.isArray((parsed as TablePayload).rows)
  ) {
    return null;
  }
  const candidate = parsed as TablePayload;
  if (!candidate.columns.every((c) => typeof c === 'string')) {
    return null;
  }
  if (!candidate.rows.every((r) => Array.isArray(r))) {
    return null;
  }
  const totalRows =
    typeof candidate.totalRows === 'number'
      ? candidate.totalRows
      : candidate.rows.length;
  return {
    columns: candidate.columns,
    rows: candidate.rows,
    totalRows,
  };
}
