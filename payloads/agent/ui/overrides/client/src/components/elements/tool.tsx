/**
 * Override of e2e-chatbot-app-next/client/src/components/elements/tool.tsx.
 *
 * Two behavioural changes:
 *
 * 1. ``ToolInput`` / ``ToolOutput``: when the input/output looks like a
 *    Databricks SQL warehouse Statement Execution call, we render a
 *    syntax-highlighted SQL block (PARAMETERS) and a paginated table
 *    (RESULT) instead of the upstream's raw JSON dump.  Detection is
 *    shape-based (no tool-name plumbing through ``message.tsx``) so the
 *    override stays surgical.
 * 2. ``ToolContainer``: panels open according to the deploy's
 *    ``spec.ui.tool_output.expanded`` mode instead of upstream's
 *    hardcoded ``defaultOpen={true}``.  This is the single chokepoint
 *    for both panel variants -- ``Tool`` aliases it and ``McpTool``
 *    (upstream ``mcp-tool.tsx``) wraps it -- so no ``message.tsx``
 *    override is needed.
 *
 * Every other export is byte-identical to upstream so the rest of the
 * chat UI keeps importing ``ToolStatusBadge`` / ``ToolContent`` /
 * ``ToolHeader`` / ``Tool`` unchanged.  See ``MANIFEST.md`` for upgrade
 * notes.
 */
import { Badge } from '@/components/ui/badge';
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from '@/components/ui/collapsible';
import { useAppConfig } from '@/contexts/AppConfigContext';
import { cn } from '@/lib/utils';
import type { ToolUIPart } from 'ai';
import {
  isValidElement,
  useContext,
  useState,
  type ComponentProps,
  type ReactNode,
} from 'react';
import { CodeBlock } from './code-block';
import { createContext } from 'react';
import { ChevronUpIcon, ShieldCheckIcon, ShieldOffIcon as ShieldXIcon, XCircleIcon, CircleOutlineIcon as CircleIcon, ClockIcon, ChevronDownIcon, WrenchIcon, CheckCircleIcon } from '../icons';
import { PaginatedTableView } from './paginated-table';
import type { TablePayload } from './paginated-table-utils';

// ---------------------------------------------------------------------------
// SQL warehouse Statement Execution detection helpers
// ---------------------------------------------------------------------------

/**
 * ``message.tsx`` wraps the raw tool output as a ``<div>`` whose only
 * child is either the output string or ``JSON.stringify(output)``.
 * Walk one level into that wrapper to recover the underlying text
 * payload so we can inspect its shape.  Anything we don't recognise
 * falls through to the upstream rendering path.
 */
function extractWrappedText(output: ReactNode): string | null {
  if (typeof output === 'string') {
    return output;
  }
  if (isValidElement(output)) {
    const child = (output.props as { children?: unknown }).children;
    if (typeof child === 'string') {
      return child;
    }
  }
  return null;
}

/**
 * The OpenAI Agents SDK (``agents.mcp.util``) collapses a single-item
 * ``result.content`` to one ``{type: "text", text: "<json>"}`` dict and
 * keeps the list when there are multiple items.  We accept both.  See
 * ``bricks/agent/tool_output_renderers.py`` for the matching server-side
 * coercion.
 */
function unwrapMcpEnvelope(output: unknown): Record<string, unknown> | null {
  if (output == null) {
    return null;
  }
  // JSON string: parse and recurse on the parsed value so we handle
  // both stringified envelope dicts AND stringified MCP content-block
  // lists (the latter is what ``message.tsx`` produces because it
  // ``JSON.stringify``s the entire output value).
  if (typeof output === 'string') {
    let parsed: unknown;
    try {
      parsed = JSON.parse(output);
    } catch {
      return null;
    }
    return unwrapMcpEnvelope(parsed);
  }
  // Single MCP text block: ``{type: "text", text: "<json>"}``.
  if (isMcpTextBlock(output)) {
    return parseTextBlock(output);
  }
  // Multi-item content list: ``[{type, text}, ...]``.  Walk the list
  // and return the first item whose text parses as a dict.
  if (Array.isArray(output)) {
    for (const item of output) {
      if (isMcpTextBlock(item)) {
        const parsed = parseTextBlock(item);
        if (parsed) {
          return parsed;
        }
      }
    }
    return null;
  }
  // Already-parsed dict: assume it IS the envelope.
  if (typeof output === 'object') {
    return output as Record<string, unknown>;
  }
  return null;
}

// MCP/Responses content-block discriminator.  ``text`` is the MCP-native
// shape; ``input_text`` is the Responses API shape that the OpenAI
// Agents SDK uses when forwarding MCP tool returns into the chat
// transcript.  Both end up in ``function_call_output.output``.
const MCP_TEXT_TYPES: ReadonlySet<string> = new Set(['text', 'input_text']);

function isMcpTextBlock(value: unknown): boolean {
  if (typeof value !== 'object' || value === null) {
    return false;
  }
  const type = (value as { type?: unknown }).type;
  const text = (value as { text?: unknown }).text;
  return typeof type === 'string'
    && MCP_TEXT_TYPES.has(type)
    && typeof text === 'string';
}

function parseTextBlock(block: unknown): Record<string, unknown> | null {
  const text = (block as { text: string }).text;
  try {
    const parsed = JSON.parse(text) as unknown;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    return null;
  }
  return null;
}

interface StatementExecutionEnvelope {
  state: string;
  columns: string[];
  rows: unknown[][];
  totalRows?: number;
}

/**
 * Pull ``(state, columns, rows)`` out of a SQL tool result.
 *
 * Accepts two shapes:
 *
 * 1. Compact ``{columns, rows, total_rows?, truncated?}`` -- what the
 *    runtime writes after auto-poll.
 * 2. Legacy Databricks Statement Execution API envelopes (and nested
 *    ``queryAttachments[].statement_response``), including value-wrapped
 *    cells, so Lakebase transcripts written before the compact deploy
 *    still render as tables.
 *
 * Returns ``null`` for any non-matching shape; callers fall back to the
 * upstream JSON dump.
 */
function extractStatementResult(
  envelope: Record<string, unknown> | null,
): StatementExecutionEnvelope | null {
  if (!envelope) {
    return null;
  }

  const compact = extractCompactTable(envelope);
  if (compact) {
    return compact;
  }

  const statement = findStatementEnvelope(envelope);
  if (!statement) {
    return null;
  }
  const status = statement.status as { state?: unknown } | undefined;
  if (typeof status?.state !== 'string') {
    return null;
  }
  const manifest = statement.manifest as
    | { schema?: { columns?: unknown } }
    | undefined;
  const colsMeta = manifest?.schema?.columns;
  if (!Array.isArray(colsMeta) || colsMeta.length === 0) {
    return null;
  }
  const columns: string[] = [];
  for (const col of colsMeta) {
    if (typeof col === 'object' && col !== null) {
      const name = (col as { name?: unknown }).name;
      if (typeof name === 'string') {
        columns.push(name);
        continue;
      }
    }
    return null;
  }
  // The Statement Execution API uses two row container fields depending on
  // ``manifest.format``: ``data_array`` for ``JSON_ARRAY`` (small results,
  // primitive types) and ``data_typed_array`` for ``PROTOBUF_ARRAY``
  // (kicks in for DECIMAL/TIMESTAMP types or larger results so precision
  // is preserved as wrapped scalar messages).  Both are inline in the
  // first chunk; we accept whichever is present.
  const result = statement.result as
    | { data_array?: unknown; data_typed_array?: unknown }
    | undefined;
  const rawRows: unknown[] = Array.isArray(result?.data_array)
    ? (result?.data_array as unknown[])
    : Array.isArray(result?.data_typed_array)
      ? (result?.data_typed_array as unknown[])
      : [];
  if (rawRows.length === 0) {
    return { state: status.state, columns, rows: [] };
  }
  const rows: unknown[][] = [];
  for (const row of rawRows) {
    rows.push(coerceRow(row, columns.length));
  }
  return { state: status.state, columns, rows };
}

/**
 * Runtime-compacted table.  Distinguished from Statement
 * Execution envelopes by the absence of ``manifest`` / ``status``.
 */
function extractCompactTable(
  envelope: Record<string, unknown>,
): StatementExecutionEnvelope | null {
  if ('manifest' in envelope || 'status' in envelope) {
    return null;
  }
  const columns = envelope.columns;
  const rows = envelope.rows;
  if (!Array.isArray(columns) || !Array.isArray(rows)) {
    return null;
  }
  if (!columns.every((c) => typeof c === 'string')) {
    return null;
  }
  if (!rows.every((r) => Array.isArray(r))) {
    return null;
  }
  const totalRows =
    typeof envelope.total_rows === 'number'
      ? envelope.total_rows
      : typeof envelope.totalRows === 'number'
        ? envelope.totalRows
        : rows.length;
  return {
    state: 'SUCCEEDED',
    columns: columns as string[],
    rows: rows as unknown[][],
    totalRows,
  };
}

function findStatementEnvelope(
  envelope: Record<string, unknown>,
): Record<string, unknown> | null {
  if (typeof (envelope.status as { state?: unknown } | undefined)?.state === 'string') {
    return envelope;
  }
  const direct = envelope.statement_response;
  if (direct && typeof direct === 'object' && !Array.isArray(direct)) {
    const nested = direct as Record<string, unknown>;
    if (typeof (nested.status as { state?: unknown } | undefined)?.state === 'string') {
      return nested;
    }
  }
  const content = envelope.content;
  const attachmentsRaw =
    Array.isArray(envelope.queryAttachments)
      ? envelope.queryAttachments
      : content && typeof content === 'object' && !Array.isArray(content)
        ? (content as { queryAttachments?: unknown }).queryAttachments
        : undefined;
  if (!Array.isArray(attachmentsRaw)) {
    return null;
  }
  for (const att of attachmentsRaw) {
    if (!att || typeof att !== 'object') {
      continue;
    }
    const sr = (att as { statement_response?: unknown }).statement_response;
    if (sr && typeof sr === 'object' && !Array.isArray(sr)) {
      const nested = sr as Record<string, unknown>;
      if (typeof (nested.status as { state?: unknown } | undefined)?.state === 'string') {
        return nested;
      }
    }
  }
  return null;
}

function coerceRow(row: unknown, expectedCols: number): unknown[] {
  if (Array.isArray(row)) {
    return row;
  }
  // ``{values: [{string_value: ...}, ...]}`` (JSON_ARRAY) or
  // ``{values: [{str|int|double|bool|...}, ...]}`` (PROTOBUF_ARRAY) ---
  // both wrap each cell as a one-key dict whose key tags the type.
  if (typeof row === 'object' && row !== null) {
    const values = (row as { values?: unknown }).values;
    if (Array.isArray(values)) {
      return values.map(extractWrappedValue);
    }
  }
  return new Array(expectedCols).fill(null);
}

// Type-tagged scalar cell wrappers used by the Statement Execution API.
// JSON_ARRAY uses the ``<type>_value`` form (string_value, long_value, ...);
// PROTOBUF_ARRAY uses short tags (str, int, double, bool, ...).  Order
// matters: the first key present wins, so list the most-common types first.
const CELL_VALUE_KEYS: readonly string[] = [
  // JSON_ARRAY long-form keys
  'string_value', 'long_value', 'double_value', 'boolean_value',
  'date_value', 'timestamp_value', 'binary_value', 'decimal_value',
  'null_value',
  // PROTOBUF_ARRAY short-form keys
  'str', 'int', 'long', 'double', 'float', 'bool', 'date',
  'timestamp', 'binary', 'decimal', 'null',
];

const NULL_CELL_KEYS: ReadonlySet<string> = new Set(['null_value', 'null']);

function extractWrappedValue(cell: unknown): unknown {
  if (cell === null || cell === undefined) {
    return null;
  }
  if (typeof cell !== 'object') {
    return cell;
  }
  const obj = cell as Record<string, unknown>;
  for (const key of CELL_VALUE_KEYS) {
    if (key in obj) {
      return NULL_CELL_KEYS.has(key) ? null : obj[key];
    }
  }
  return cell;
}

function envelopeAsTablePayload(
  envelope: StatementExecutionEnvelope,
): TablePayload {
  return {
    columns: envelope.columns,
    rows: envelope.rows,
    totalRows: envelope.totalRows ?? envelope.rows.length,
  };
}

// SQL openers paired with the structural follow-up tokens that English
// natural-language never uses.  Natural-language openers like "Show me a
// summary of all schemas..." trip a bare-keyword regex (SHOW), so we
// require the second token to be a SQL-specific noun keyword (TABLES /
// SCHEMAS / FROM / etc.) or a quoted/backticked identifier.
//
// First match group covers the common SELECT/WITH/INSERT/... shapes:
//   ``WITH foo AS``
//   ``SELECT col, ...``
//   ``SELECT *`` / ``SELECT 1`` (literal/star)
//   ``INSERT INTO`` / ``UPDATE x SET`` / ``DELETE FROM`` / ``MERGE INTO``
//   ``CREATE TABLE`` / ``ALTER`` / ``DROP`` / ``TRUNCATE`` (DDL)
//   ``DESCRIBE TABLE foo`` / ``DESC foo``
//   ``SHOW TABLES`` / ``SHOW SCHEMAS`` / ``SHOW CATALOGS`` / etc. (the
//      list mirrors Spark/Databricks ``SHOW <noun>`` grammar)
//   ``EXPLAIN <stmt>`` / ``USE`` / ``SET`` / ``REFRESH`` / ``OPTIMIZE`` /
//   ``ANALYZE`` / ``VACUUM`` / ``COPY INTO`` / ``CALL`` / ``VALUES``
const SQL_STATEMENT_RE = new RegExp(
  '^\\s*(?:--[^\\n]*\\n|\\/\\*[\\s\\S]*?\\*\\/\\s*)*(?:'
    + 'WITH\\s+\\S+\\s+AS\\b'
    + '|SELECT\\s+(?:\\*|DISTINCT\\b|ALL\\b|TOP\\s+\\d|\\d|`|"|\'|\\w+\\s*[,(.])'
    + '|SELECT\\s+\\w+(?:\\s*,\\s*\\w+)*\\s+FROM\\b'
    + '|INSERT\\s+INTO\\b'
    + '|UPDATE\\s+\\S+\\s+SET\\b'
    + '|DELETE\\s+FROM\\b'
    + '|MERGE\\s+INTO\\b'
    + '|(?:CREATE|ALTER|DROP|TRUNCATE)\\s+(?:TABLE|VIEW|SCHEMA|DATABASE|INDEX|FUNCTION|VOLUME|CATALOG|SHARE|RECIPIENT|CONNECTION|TEMPORARY|EXTERNAL|MATERIALIZED|OR\\s+REPLACE)\\b'
    + '|DESC(?:RIBE)?\\s+(?:TABLE|EXTENDED|FUNCTION|HISTORY|SCHEMA|DATABASE|CATALOG|VOLUME|SHARE|`|")'
    + '|DESC(?:RIBE)?\\s+\\S+\\.\\S+'
    + '|SHOW\\s+(?:TABLES|TABLE|VIEWS|SCHEMAS|DATABASES|CATALOGS|FUNCTIONS|GRANTS|COLUMNS|PARTITIONS|TBLPROPERTIES|USERS|GROUPS|ROLES|VOLUMES|SHARES|RECIPIENTS|CONNECTIONS|CREATE)\\b'
    + '|EXPLAIN\\s+(?:SELECT|INSERT|UPDATE|DELETE|MERGE|WITH|EXTENDED|FORMATTED)\\b'
    + '|USE\\s+(?:CATALOG|SCHEMA|DATABASE|`|")'
    + '|SET\\s+(?:TIMEZONE|TIME\\s+ZONE|`|"|\\w+\\s*=)'
    + '|REFRESH\\s+(?:TABLE|FUNCTION)\\b'
    + '|OPTIMIZE\\s+\\S'
    + '|ANALYZE\\s+TABLE\\b'
    + '|VACUUM\\s+\\S'
    + '|COPY\\s+INTO\\b'
    + '|CALL\\s+\\S+\\.'
    + '|VALUES\\s*\\('
    + ')',
  'i',
);

function looksLikeSql(value: string): boolean {
  return SQL_STATEMENT_RE.test(value);
}

/**
 * Pull a SQL ``query`` string out of the tool input, if present.
 *
 * Recognises the common argument names used by Databricks SQL tools
 * (``query`` / ``statement`` / ``sql``).  The argument value is also
 * sniffed against a SQL-keyword regex -- Genie's ``query_space`` tool
 * uses the same ``query`` argument name to carry a natural-language
 * question, so we want to render that as plain text, not as SQL.
 */
function extractSqlQuery(input: unknown): string | null {
  if (typeof input !== 'object' || input === null) {
    return null;
  }
  const obj = input as Record<string, unknown>;
  for (const key of ['query', 'statement', 'sql']) {
    const value = obj[key];
    if (typeof value === 'string' && value.trim().length > 0) {
      return looksLikeSql(value) ? value : null;
    }
  }
  return null;
}

// ---------------------------------------------------------------------------
// Upstream surface (verbatim from e2e-chatbot-app-next/client/src/components/
// elements/tool.tsx, except where noted)
// ---------------------------------------------------------------------------

// Shared types - uses AI SDK's native tool states
export type ToolState = ToolUIPart['state'];

// Shared status badge component
type ToolStatusBadgeProps = {
  state: ToolState;
  className?: string;
};

export const ToolStatusBadge = ({ state, className }: ToolStatusBadgeProps) => {
  const labels: Record<ToolState, string> = {
    'input-streaming': 'Pending',
    'input-available': 'Running',
    'output-available': 'Completed',
    'output-error': 'Error',
    'output-denied': 'Denied',
    'approval-requested': 'Approval Requested',
    'approval-responded': 'Processing',
  };

  const icons: Record<ToolState, ReactNode> = {
    'input-streaming': <CircleIcon className="size-3" />,
    'input-available': <ClockIcon className="size-3 animate-pulse" />,
    'output-available': <CheckCircleIcon className="size-3" />,
    'output-error': <XCircleIcon className="size-3" />,
    'output-denied': <ShieldXIcon className="size-3" />,
    'approval-requested': <ShieldXIcon className="size-3" />,
    'approval-responded': <ShieldCheckIcon className="size-3" />,
  };

  const variants: Record<ToolState, string> = {
    'input-streaming':
      'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300',
    'input-available':
      'bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300',
    'output-available':
      'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300',
    'output-error': 'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300',
    'output-denied':
      'bg-red-100 text-red-700 dark:bg-red-900 dark:text-red-300',
    'approval-requested':
      'bg-amber-100 text-amber-700 dark:bg-amber-900 dark:text-amber-300',
    'approval-responded':
      'bg-green-100 text-green-700 dark:bg-green-900 dark:text-green-300',
  };

  return (
    <Badge
      className={cn(
        'flex items-center gap-1 rounded-full border-0 font-medium text-xs',
        variants[state],
        className,
      )}
      variant="secondary"
    >
      {icons[state]}
      <span>{labels[state]}</span>
    </Badge>
  );
};

// Shared container component
type ToolContainerProps = ComponentProps<typeof Collapsible>;

const ToolContext = createContext<{
  open: boolean;
}>({
  open: false,
});

export const ToolContainer = ({
  className,
  defaultOpen: _defaultOpen,
  ...props
}: ToolContainerProps) => {
  const { toolOutputExpanded } = useAppConfig();
  // ``null`` until the user clicks a header, so the deploy-time default
  // still applies to panels that mounted before /api/config resolved.
  // Upstream's ``defaultOpen`` is dropped rather than forwarded: both
  // call sites in message.tsx hardcode ``true``, which would pin every
  // panel open regardless of the spec.
  const [userOpen, setUserOpen] = useState<boolean | null>(null);
  const open = userOpen ?? toolOutputExpanded;
  return (
    <ToolContext.Provider value={{ open }}>
      <Collapsible
        className={cn('not-prose w-full rounded-xl border', className)}
        open={open}
        onOpenChange={setUserOpen}
        {...props}
      /></ToolContext.Provider>
  );
}

// Shared collapsible content component
type ToolContentProps = ComponentProps<typeof CollapsibleContent>;

export const ToolContent = ({ className, ...props }: ToolContentProps) => (
  <CollapsibleContent
    className={cn(
      'data-[state=closed]:fade-out-0 data-[state=closed]:slide-out-to-top-2 data-[state=open]:slide-in-from-top-2 text-popover-foreground outline-hidden data-[state=closed]:animate-out data-[state=open]:animate-in',
      className,
    )}
    {...props}
  />
);

// Shared input component
//
// Override: when the parameters object carries a ``query`` (or
// ``statement`` / ``sql``) string, render the SQL with syntax
// highlighting instead of a JSON dump.  Anything else falls through to
// the upstream JSON rendering.
type ToolInputProps = ComponentProps<'div'> & {
  input: ToolUIPart['input'];
};

export const ToolInput = ({ className, input, ...props }: ToolInputProps) => {
  const sql = extractSqlQuery(input);
  return (
    <div className={cn('space-y-2 overflow-hidden p-3', className)} {...props}>
      <h4 className="font-medium text-muted-foreground text-xs uppercase tracking-wide">
        {sql ? 'SQL' : 'Parameters'}
      </h4>
      <div className="rounded-md bg-muted/50">
        {sql ? (
          <CodeBlock code={sql} language="sql" />
        ) : (
          <CodeBlock code={JSON.stringify(input, null, 2)} language="json" />
        )}
      </div>
    </div>
  );
};

// Shared output component
//
// Override: when the tool output unwraps to a Statement Execution API
// envelope, render the rows as a paginated table.  Anything else falls
// through to the upstream raw rendering.
type ToolOutputProps = ComponentProps<'div'> & {
  output: ReactNode;
  errorText?: string;
};

export const ToolOutput = ({
  className,
  output,
  errorText,
  ...props
}: ToolOutputProps) => {
  if (!(output || errorText)) {
    return null;
  }

  // Detect Statement Execution envelope.  ``message.tsx`` passes us a
  // ``<div>`` wrapping the JSON-stringified tool output, so we walk one
  // level into that wrapper before unwrapping the MCP content-block
  // shape.  Anything we don't recognise falls through to the upstream
  // rendering below.
  const wrappedText = errorText ? null : extractWrappedText(output);
  const envelope = wrappedText
    ? extractStatementResult(unwrapMcpEnvelope(wrappedText))
    : null;

  return (
    <div className={cn('space-y-2 p-3', className)} {...props}>
      <h4 className="font-medium text-muted-foreground text-xs uppercase tracking-wide">
        {errorText ? 'Error' : 'Result'}
      </h4>
      {envelope ? (
        <PaginatedTableView payload={envelopeAsTablePayload(envelope)} />
      ) : (
        <div
          className={cn(
            'overflow-x-auto rounded-md text-xs [&_table]:w-full',
            errorText
              ? 'bg-destructive/10 text-destructive'
              : 'bg-muted/50 text-foreground',
          )}
        >
          {errorText && <div className="p-2">{errorText}</div>}
          {output && <div>{output}</div>}
        </div>
      )}
    </div>
  );
};

// Standard tool components (non-MCP)
export const Tool = ToolContainer;

type ToolHeaderProps = {
  type: ToolUIPart['type'] | string;
  state: ToolState;
  className?: string;
};

export const ToolHeader = ({
  className,
  type,
  state,
  ...props
}: ToolHeaderProps) => {
  const { open } = useContext(ToolContext);
  return (
    <CollapsibleTrigger
      className={cn(
        'flex w-full min-w-0 items-center justify-between gap-2 p-3 cursor-pointer',
        className,
      )}
      {...props}
    >
      <div className="flex min-w-0 flex-1 items-center gap-2">
        <WrenchIcon className="size-4 shrink-0 text-muted-foreground" />
        <span className="truncate font-medium text-sm">{type}</span>
      </div>
      <div className="flex shrink-0 items-center gap-2">
        <ToolStatusBadge state={state} />
        {open ? <ChevronUpIcon /> : <ChevronDownIcon />}
        {/* <ChevronDownIcon className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-180" /> */}
      </div>
    </CollapsibleTrigger>
  );
}
