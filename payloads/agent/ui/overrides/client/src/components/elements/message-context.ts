/**
 * Shared helpers for fence-rendered elements (sql-code-block,
 * paginated-table, json-download).
 *
 * Design adapted from
 * https://github.com/databricks-solutions/dbx-unifiedchat
 * at commit e1e1732b1c0a5b14b0208898ebaab71119164498
 * (Databricks License — redistribution permitted within Databricks
 * Services with attribution).
 */

/**
 * Decode a base64-encoded string as UTF-8.  Falls back to the raw
 * input if decoding fails so a malformed fence header degrades to
 * "render the literal payload" rather than a crash.
 */
export function decodeBase64Utf8(value: string): string {
  try {
    const binary = atob(value);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i += 1) {
      bytes[i] = binary.charCodeAt(i);
    }
    return new TextDecoder('utf-8').decode(bytes);
  } catch {
    return value;
  }
}

/**
 * Parse a fence header of the form ``language-<tag>:<filename>:<base64>``.
 *
 * Returns ``null`` when the header doesn't carry the two expected
 * trailing segments — callers fall through to the upstream code block.
 */
export function parseFenceHeader(
  className: string,
  expectedTag: string,
): { filename: string; payload: string } | null {
  const prefix = `language-${expectedTag}:`;
  const idx = className.indexOf(prefix);
  if (idx === -1) {
    return null;
  }
  const remainder = className.slice(idx + prefix.length).split(/\s/)[0];
  const sep = remainder.indexOf(':');
  if (sep <= 0) {
    return null;
  }
  return {
    filename: remainder.slice(0, sep),
    payload: remainder.slice(sep + 1),
  };
}

function clickDownloadLink(filename: string, href: string): void {
  const link = document.createElement('a');
  link.href = href;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
}

/**
 * Trigger a browser download for the given text content.
 * Used by ``sql-download``, ``table-download`` (CSV), and
 * ``json-download`` toolbars.
 */
export function downloadTextFile(
  filename: string,
  content: string,
  mimeType = 'text/plain;charset=utf-8',
): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  clickDownloadLink(filename, url);
  URL.revokeObjectURL(url);
}

/** Trigger a browser download for a ``data:`` URL (chart PNG export). */
export function downloadDataUrl(filename: string, dataUrl: string): void {
  clickDownloadLink(filename, dataUrl);
}

/**
 * Serialise rows to CSV with RFC-4180 quoting.
 */
export function rowsToCsv(
  columns: string[],
  rows: ReadonlyArray<ReadonlyArray<unknown>>,
): string {
  const escapeCell = (value: unknown): string => {
    const str = value == null ? '' : String(value);
    if (/[",\n\r]/.test(str)) {
      return `"${str.replace(/"/g, '""')}"`;
    }
    return str;
  };
  const header = columns.map(escapeCell).join(',');
  const body = rows.map((row) => row.map(escapeCell).join(',')).join('\n');
  return body ? `${header}\n${body}\n` : `${header}\n`;
}
