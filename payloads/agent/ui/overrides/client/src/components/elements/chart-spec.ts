/**
 * Chart spec schema + ECharts option builder + CSV serializer for the
 * ``echarts-chart`` fence renderer.
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

import { rowsToCsv } from './message-context';

export const MAX_DATA_ROWS = 500;

export const CHART_TYPES = [
  'bar',
  'line',
  'area',
  'pie',
  'scatter',
] as const;

// Explicit default palette.  ECharts 6 ships a new default theme; if
// the spec doesn't supply ``palette`` and we leave ``color: undefined``
// the series can resolve to ``transparent`` swatches under the bundled
// build's specific runtime, producing invisible (but otherwise valid)
// bars and lines.  Hard-coding a 13-color palette guarantees every
// series has a visible color, regardless of the theme version that
// happens to be loaded.  Colors are picked to be perceptually distinct
// and keep the v5 first-two (blue/green) for visual continuity.
export const DEFAULT_PALETTE: readonly string[] = [
  '#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de',
  '#3ba272', '#fc8452', '#9a60b4', '#ea7ccc', '#ffb6c1',
  '#a6cee3', '#1f78b4', '#b2df8a',
];

export type ChartType = (typeof CHART_TYPES)[number];

const dataRowSchema = z.record(z.string(), z.unknown());

export const chartSpecSchema = z
  .object({
    type: z.enum(CHART_TYPES),
    title: z.string().optional(),
    data: z.array(dataRowSchema).max(MAX_DATA_ROWS),
    x: z.string(),
    y: z.array(z.string()).min(1),
    groupBy: z.string().optional(),
    aggregation: z.enum(['sum', 'avg', 'min', 'max', 'count']).optional(),
    palette: z.array(z.string()).optional(),
  })
  .strict();

export type ChartSpec = z.infer<typeof chartSpecSchema>;

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

interface BuildOptionInput {
  spec: ChartSpec;
  /** Override chart type (used by the in-chart toolbar's type swap). */
  typeOverride?: ChartType;
  /** Drill-down filter — show only rows whose `x` value matches. */
  drillFilter?: string | null;
}

/**
 * Build an ECharts option object from a validated spec.
 *
 * Pure function — no DOM access, no React, no lazy ECharts import.
 */
export function buildEChartsOption({
  spec,
  typeOverride,
  drillFilter,
}: BuildOptionInput): Record<string, unknown> {
  const effectiveType = typeOverride ?? spec.type;
  const data = drillFilter != null
    ? spec.data.filter((row) => String(row[spec.x]) === drillFilter)
    : spec.data;

  if (effectiveType === 'pie') {
    const series = spec.y[0];
    return {
      title: spec.title ? { text: spec.title, left: 'center' } : undefined,
      tooltip: { trigger: 'item' },
      legend: { bottom: 0 },
      series: [
        {
          type: 'pie',
          radius: ['35%', '65%'],
          data: data.map((row) => ({
            name: String(row[spec.x] ?? ''),
            value: Number(row[series]) || 0,
          })),
        },
      ],
      color: spec.palette ?? DEFAULT_PALETTE,
    };
  }

  const xValues = data.map((row) => String(row[spec.x] ?? ''));
  const seriesType = effectiveType === 'area' ? 'line' : effectiveType;
  const series = spec.y.map((field) => {
    const values = data.map((row) => {
      const raw = row[field];
      return typeof raw === 'number' ? raw : Number(raw);
    });
    const base: Record<string, unknown> = {
      name: field,
      type: seriesType,
      data: values.map((v) => (Number.isFinite(v) ? v : null)),
    };
    if (effectiveType === 'area') {
      base.areaStyle = {};
    }
    if (effectiveType === 'scatter') {
      base.symbolSize = 10;
    }
    return base;
  });

  // A scrollable legend is required for multi-series charts: the
  // default layout collapses to ``bottom: 0`` and wraps onto multiple
  // rows, which silently overlaps the x-axis and visually swallows the
  // bars.  ``type: 'scroll'`` keeps the legend on a single row with
  // arrows; ``grid.bottom`` is raised to leave room for it.
  const hasMultiSeries = spec.y.length > 1;
  return {
    title: spec.title ? { text: spec.title, left: 'center' } : undefined,
    tooltip: { trigger: 'axis' },
    legend: hasMultiSeries ? { bottom: 0, type: 'scroll' } : undefined,
    grid: {
      left: 48,
      right: 16,
      top: spec.title ? 48 : 16,
      bottom: hasMultiSeries ? 56 : 48,
      containLabel: true,
    },
    xAxis: { type: 'category', data: xValues },
    yAxis: { type: 'value' },
    series,
    color: spec.palette ?? DEFAULT_PALETTE,
  };
}

/**
 * Flatten a chart spec to CSV rows for download.  Columns are
 * ``[x, ...y]``; missing values render as empty cells.
 */
export function serializeToCsv(spec: ChartSpec): string {
  const columns = [spec.x, ...spec.y];
  const rows = spec.data.map(
    (row) => columns.map((c) => row[c] ?? ''),
  );
  return rowsToCsv(columns, rows);
}
