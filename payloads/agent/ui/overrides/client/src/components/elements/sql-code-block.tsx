/**
 * Compact SQL / JSON code-block renderer with Copy + Download.
 *
 * Used by the ``sql-download`` and ``json-download`` fence handlers in
 * ``response.tsx``.  Design adapted from
 * https://github.com/databricks-solutions/dbx-unifiedchat
 * at commit e1e1732b1c0a5b14b0208898ebaab71119164498.
 */
import { useCallback, useState } from 'react';
import { Copy, Check, Download } from 'lucide-react';

import { downloadTextFile } from './message-context';

interface CodeBlockWithActionsProps {
  filename: string;
  content: string;
  language: 'sql' | 'json';
}

const CONTENT_TYPE_BY_LANGUAGE: Record<'sql' | 'json', string> = {
  sql: 'application/sql;charset=utf-8',
  json: 'application/json;charset=utf-8',
};

const COPY_FEEDBACK_MS = 1500;

export function CodeBlockWithActions({
  filename,
  content,
  language,
}: CodeBlockWithActionsProps) {
  const [copied, setCopied] = useState(false);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), COPY_FEEDBACK_MS);
    } catch {
      // Clipboard API unavailable (e.g. insecure context) — silently no-op
      // rather than throwing inside a render path.
    }
  }, [content]);

  const handleDownload = useCallback(() => {
    downloadTextFile(
      filename || `snippet.${language}`,
      content,
      CONTENT_TYPE_BY_LANGUAGE[language],
    );
  }, [filename, content, language]);

  return (
    <div className="my-2 rounded-md border border-border bg-muted/30">
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-1.5">
        <span className="text-xs uppercase tracking-wide text-muted-foreground">
          {language}
        </span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={handleCopy}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs hover:bg-muted"
            aria-label={copied ? 'Copied' : 'Copy'}
          >
            {copied ? (
              <Check className="h-3.5 w-3.5" />
            ) : (
              <Copy className="h-3.5 w-3.5" />
            )}
            {copied ? 'Copied' : 'Copy'}
          </button>
          <button
            type="button"
            onClick={handleDownload}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs hover:bg-muted"
            aria-label="Download"
          >
            <Download className="h-3.5 w-3.5" />
            Download
          </button>
        </div>
      </div>
      <pre className="overflow-x-auto px-3 py-2 text-xs">
        <code className={`language-${language}`}>{content}</code>
      </pre>
    </div>
  );
}

export default CodeBlockWithActions;
