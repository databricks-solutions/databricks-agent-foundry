/**
 * Chart spec schema + CSV serializer for the ``echarts-chart`` fence
 * renderer.  The ECharts option builder lives in ``chart-option.ts``.
 *
 * Design adapted from
 * https://github.com/databricks-solutions/dbx-unifiedchat
 * at commit e1e1732b1c0a5b14b0208898ebaab71119164498
 * (Databricks License — redistribution permitted within Databricks
 * Services with attribution).  Trimmed to the read-only Option A
 * surface; customizer / multi-axis / NL-rechart features are
 * deliberately omitted (see issue #105 "Out of scope").
 */
import { z } from 'zod';

import { CHART_TYPES, layoutErrors } from './chart-option';
import { rowsToCsv } from './message-context';

export const MAX_DATA_ROWS = 500;
export const MAX_AXIS_NAME_LENGTH = 60;
export const MAX_REFERENCE_MARKS = 5;

const dataRowSchema = z.record(z.string(), z.unknown());
const axisNameSchema = z.string().max(MAX_AXIS_NAME_LENGTH);
const markAxisSchema = z.enum(['x', 'y']);
const markValueSchema = z.union([z.number(), z.string()]);

const referenceLineSchema = z
  .object({
    axis: markAxisSchema,
    value: markValueSchema,
    label: axisNameSchema.optional(),
  })
  .strict();

const referenceBandSchema = z
  .object({
    axis: markAxisSchema,
    from: markValueSchema,
    to: markValueSchema,
    label: axisNameSchema.optional(),
  })
  .strict();

const yAxisSchema = z
  .object({
    name: axisNameSchema.optional(),
    log: z.boolean().optional(),
    min: z.number().optional(),
    max: z.number().optional(),
  })
  .strict();

const chartSpecObjectSchema = z
  .object({
    type: z.enum(CHART_TYPES),
    title: z.string().optional(),
    data: z.array(dataRowSchema).max(MAX_DATA_ROWS),
    x: z.string(),
    y: z.array(z.string()).min(1),
    palette: z.array(z.string()).optional(),
    xType: z.enum(['category', 'time']).optional(),
    xName: axisNameSchema.optional(),
    yAxis: yAxisSchema.optional(),
    stacked: z.boolean().optional(),
    horizontal: z.boolean().optional(),
    y2: z.array(z.string()).min(1).optional(),
    y2Axis: yAxisSchema.optional(),
    size: z.string().optional(),
    target: z.string().optional(),
    referenceLines: z.array(referenceLineSchema).max(MAX_REFERENCE_MARKS).optional(),
    referenceBands: z.array(referenceBandSchema).max(MAX_REFERENCE_MARKS).optional(),
  })
  .strict();

export type ChartSpec = z.infer<typeof chartSpecObjectSchema>;

export const chartSpecSchema = chartSpecObjectSchema.superRefine((spec, ctx) => {
  for (const message of layoutErrors(spec)) {
    ctx.addIssue({ code: 'custom', message });
  }
});

/**
 * Parse a JSON string into a validated ``ChartSpec``.  Returns ``null``
 * on any structural failure so the caller (``response.tsx``) can fall
 * back to the upstream code block.
 */
export function parseChartSpec(json: string): ChartSpec | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    return null;
  }
  const result = chartSpecSchema.safeParse(parsed);
  return result.success ? result.data : null;
}

/**
 * Flatten a chart spec to CSV rows for download.  Columns are
 * ``[x, target, ...y, ...y2, size]``; missing values render as empty cells.
 */
export function serializeToCsv(spec: ChartSpec): string {
  const columns = [
    spec.x,
    ...(spec.target ? [spec.target] : []),
    ...spec.y,
    ...(spec.y2 ?? []),
    ...(spec.size ? [spec.size] : []),
  ];
  const rows = spec.data.map(
    (row) => columns.map((c) => row[c] ?? ''),
  );
  return rowsToCsv(columns, rows);
}
