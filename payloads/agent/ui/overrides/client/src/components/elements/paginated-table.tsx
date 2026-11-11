/**
 * Paginated, downloadable table renderer for ``table-download`` fences.
 *
 * Design adapted from
 * https://github.com/databricks-solutions/dbx-unifiedchat
 * at commit e1e1732b1c0a5b14b0208898ebaab71119164498
 * (Databricks License — redistribution permitted within Databricks
 * Services with attribution).  The original used ``ag-grid``; this
 * implementation uses ``@tanstack/react-table`` to keep the bundle
 * small and the licence MIT.  Lazy-loaded by ``response.tsx`` so the
 * table-core code only ships once the user actually receives a
 * ``table-download`` fence.
 */
import { useMemo, useState } from 'react';
import {
  type ColumnDef,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { ChevronLeft, ChevronRight, Download } from 'lucide-react';

import { downloadTextFile, rowsToCsv } from './message-context';
import {
  type TablePayload,
  parseTablePayload,
} from './paginated-table-utils';

const DEFAULT_PAGE_SIZE = 25;
const MIME_CSV = 'text/csv;charset=utf-8';

interface PaginatedTableProps {
  filename: string;
  payloadB64: string;
}

interface PaginatedTableViewProps {
  filename?: string;
  payload: TablePayload;
}

function formatCell(value: unknown): string {
  if (value == null) {
    return '';
  }
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function buildColumns(
  columns: string[],
): ColumnDef<unknown[]>[] {
  return columns.map((name, index) => ({
    id: `col-${index}`,
    header: name,
    accessorFn: (row: unknown[]) => row[index],
    cell: (ctx) => formatCell(ctx.getValue()),
  }));
}

/**
 * Thin wrapper that decodes a base64-encoded ``table-download`` payload
 * and forwards to ``PaginatedTableView`` once parsed.  Used by the
 * fence-dispatcher in ``response.tsx``.
 */
export function PaginatedTable({ filename, payloadB64 }: PaginatedTableProps) {
  const payload: TablePayload | null = useMemo(
    () => parseTablePayload(payloadB64),
    [payloadB64],
  );
  if (!payload) {
    return null;
  }
  return <PaginatedTableView filename={filename} payload={payload} />;
}

/**
 * Render a paginated, downloadable table from an already-parsed
 * payload.  Used by the Tool panel override (``tool.tsx``) which has
 * the structured envelope in hand and doesn't need base64 decoding.
 */
export function PaginatedTableView({
  filename,
  payload,
}: PaginatedTableViewProps) {
  const [pageIndex, setPageIndex] = useState(0);

  const columns = useMemo(
    () => buildColumns(payload.columns),
    [payload.columns],
  );

  const table = useReactTable({
    data: payload.rows,
    columns,
    state: {
      pagination: { pageIndex, pageSize: DEFAULT_PAGE_SIZE },
    },
    onPaginationChange: (updater) => {
      const next =
        typeof updater === 'function'
          ? updater({ pageIndex, pageSize: DEFAULT_PAGE_SIZE })
          : updater;
      setPageIndex(next.pageIndex);
    },
    getCoreRowModel: getCoreRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
  });

  const handleDownload = () => {
    const csv = rowsToCsv(payload.columns, payload.rows);
    downloadTextFile(filename || 'result.csv', csv, MIME_CSV);
  };

  const totalPages = Math.max(1, table.getPageCount());
  const currentPage = pageIndex + 1;

  return (
    <div className="my-2 rounded-md border border-border bg-background text-sm">
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
        <span className="text-xs text-muted-foreground">
          {payload.totalRows.toLocaleString()} row
          {payload.totalRows === 1 ? '' : 's'}
        </span>
        <button
          type="button"
          onClick={handleDownload}
          className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs hover:bg-muted"
          aria-label="Download CSV"
        >
          <Download className="h-3.5 w-3.5" />
          CSV
        </button>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full border-collapse">
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id} className="bg-muted/40">
                {group.headers.map((header) => (
                  <th
                    key={header.id}
                    className="border-b border-border px-3 py-2 text-left text-xs font-medium"
                  >
                    {header.isPlaceholder
                      ? null
                      : flexRender(
                          header.column.columnDef.header,
                          header.getContext(),
                        )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id} className="hover:bg-muted/30">
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className="border-b border-border/50 px-3 py-1.5 align-top font-mono text-xs"
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {totalPages > 1 && (
        <div className="flex items-center justify-end gap-2 border-t border-border px-3 py-2 text-xs">
          <button
            type="button"
            onClick={() => table.previousPage()}
            disabled={!table.getCanPreviousPage()}
            className="inline-flex h-6 w-6 items-center justify-center rounded hover:bg-muted disabled:opacity-40"
            aria-label="Previous page"
          >
            <ChevronLeft className="h-3.5 w-3.5" />
          </button>
          <span className="tabular-nums text-muted-foreground">
            {currentPage} / {totalPages}
          </span>
          <button
            type="button"
            onClick={() => table.nextPage()}
            disabled={!table.getCanNextPage()}
            className="inline-flex h-6 w-6 items-center justify-center rounded hover:bg-muted disabled:opacity-40"
            aria-label="Next page"
          >
            <ChevronRight className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </div>
  );
}

export default PaginatedTable;
