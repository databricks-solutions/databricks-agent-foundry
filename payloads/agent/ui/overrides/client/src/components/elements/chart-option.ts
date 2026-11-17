/**
 * ECharts option builder for the ``echarts-chart`` fence renderer.
 *
 * Kept free of runtime dependencies (no zod, no ECharts, no React) so
 * it can be unit-tested under plain ``node:test``.  The schema that
 * validates a ``ChartSpec`` lives in ``chart-spec.ts``.
 *
 * Design adapted from
 * https://github.com/databricks-solutions/dbx-unifiedchat
 * at commit e1e1732b1c0a5b14b0208898ebaab71119164498
 * (Databricks License — redistribution permitted within Databricks
 * Services with attribution).
 */
import type { ChartSpec } from './chart-spec';

export const CHART_TYPES = [
  'bar',
  'line',
  'area',
  'pie',
  'scatter',
  'sankey',
] as const;

export type ChartType = (typeof CHART_TYPES)[number];

export const STACKABLE_TYPES: readonly ChartType[] = ['bar', 'area'];
export const HORIZONTAL_TYPES: readonly ChartType[] = ['bar'];
/** Types the toolbar can switch between; flow data fits no other type. */
export const SWITCHABLE_TYPES: readonly ChartType[] = ['bar', 'line', 'area', 'pie', 'scatter'];
/** Types where clicking a category filters the chart to it. */
export const DRILLABLE_TYPES: readonly ChartType[] = ['bar', 'line', 'area', 'scatter'];

/** More nodes than this make a sankey unreadable. */
export const MAX_SANKEY_NODES = 40;
/** Options a sankey has no axis or series for. */
const SANKEY_UNSUPPORTED = ['xType', 'xName', 'yAxis', 'y2', 'y2Axis', 'referenceLines', 'referenceBands'] as const;
const SANKEY_LABEL_MAX_PX = 120;
/** One label line, so labels of neighbouring nodes never overlap however small the nodes are. */
const SANKEY_NODE_GAP_PX = 14;
/** Height a sankey needs per node in its fullest column, gap included. */
const SANKEY_PX_PER_NODE = 22;
/** Title room above and margin below the sankey. */
const SANKEY_VERTICAL_MARGIN_PX = 64;

export const CHART_HEIGHT_PX = 320;

/** Above this many x values the chart gets a zoom slider. */
export const DATA_ZOOM_THRESHOLD = 50;

export const BUBBLE_MIN_PX = 6;
export const BUBBLE_MAX_PX = 40;

/** Scatter points overlap; partial transparency keeps covered points visible. */
const SCATTER_OPACITY = 0.7;
/** Room past the outermost scatter points so they are not cut in half at the grid edge. */
const SCATTER_AXIS_PADDING = ['5%', '5%'] as const;

/** Longer axis names end in an ellipsis instead of squeezing the plot. */
export const AXIS_NAME_MAX_PX = 200;
/** Up to this many categories every label is shown, shortened to fit, instead of thinned out. */
export const ALL_LABELS_MAX_CATEGORIES = 12;
/** Estimated plot width the category labels share; the real width is unknown here. */
const CATEGORY_LABELS_BUDGET_PX = 600;
const CATEGORY_LABEL_MAX_PX = 160;
/** Longer titles end in an ellipsis; a centred title cannot wrap without covering the plot. */
export const TITLE_MAX_CHARS = 70;

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

type DataRow = ChartSpec['data'][number];
type ValueAxisSpec = NonNullable<ChartSpec['yAxis']>;
type ReferenceLine = NonNullable<ChartSpec['referenceLines']>[number];
type ReferenceBand = NonNullable<ChartSpec['referenceBands']>[number];
type MarkValue = ReferenceLine['value'];
type EChartsObject = Record<string, unknown>;

interface SankeyLink {
  source: string;
  target: string;
  value: number;
}

/** How the x values are placed: evenly as labels, on a time scale, or by numeric value. */
type XMode = 'category' | 'time' | 'value';

interface Layout {
  xMode: XMode;
  horizontal: boolean;
  stacked: boolean;
  zoom: boolean;
  multiSeries: boolean;
  /** Series on the right-hand axis; empty when the chart has none. */
  y2Fields: readonly string[];
  /** Field that sets bubble size; only set for scatter charts. */
  sizeField: string | undefined;
}

interface SeriesContext {
  spec: ChartSpec;
  type: ChartType;
  layout: Layout;
  data: readonly DataRow[];
  /** Per-row bubble size in pixels, aligned with ``data``. */
  sizes?: readonly number[];
}

export interface BuildOptionInput {
  spec: ChartSpec;
  /** Override chart type (used by the in-chart toolbar's type swap). */
  typeOverride?: ChartType;
  /** Drill-down filter — show only rows whose `x` value matches. */
  drillFilter?: string | null;
}

function layoutTypeErrors(spec: ChartSpec): string[] {
  const errors: string[] = [];
  if (spec.horizontal && !HORIZONTAL_TYPES.includes(spec.type)) {
    errors.push('horizontal requires type bar');
  }
  if (spec.horizontal && spec.xType === 'time') {
    errors.push('horizontal does not support xType time');
  }
  if (spec.stacked && !STACKABLE_TYPES.includes(spec.type)) {
    errors.push('stacked requires type bar or area');
  }
  // On a log scale stacked segment heights no longer add up visually.
  if (spec.stacked && spec.yAxis?.log) {
    errors.push('stacked does not support a log y axis');
  }
  if (spec.xType === 'time' && spec.type === 'pie') {
    errors.push('xType time is not supported for pie');
  }
  if (spec.size && spec.type !== 'scatter') {
    errors.push('size requires type scatter');
  }
  return errors;
}

function axisBoundErrors(label: string, axis: ValueAxisSpec | undefined): string[] {
  const errors: string[] = [];
  const { log, min, max } = axis ?? {};
  if (log && min !== undefined && min <= 0) {
    errors.push(`${label}.min must be greater than 0 on a log axis`);
  }
  if (min !== undefined && max !== undefined && min >= max) {
    errors.push(`${label}.min must be less than ${label}.max`);
  }
  return errors;
}

function y2Errors(spec: ChartSpec): string[] {
  if (!spec.y2) {
    return spec.y2Axis ? ['y2Axis requires y2'] : [];
  }
  const errors: string[] = [];
  if (spec.type === 'pie') {
    errors.push('y2 is not supported for pie');
  }
  if (spec.horizontal) {
    errors.push('y2 does not support horizontal');
  }
  if (spec.stacked) {
    errors.push('y2 does not support stacked');
  }
  if (spec.y2.some((field) => spec.y.includes(field))) {
    errors.push('y2 fields must not repeat y fields');
  }
  return errors;
}

function valueMarkErrors(label: string, values: readonly MarkValue[], log: boolean): string[] {
  if (values.some((value) => typeof value !== 'number')) {
    return [`${label} on the y axis must be numeric`];
  }
  if (log && values.some((value) => typeof value === 'number' && value <= 0)) {
    return [`${label} on the y axis must be greater than 0 on a log axis`];
  }
  return [];
}

function referenceErrors(spec: ChartSpec): string[] {
  const lines = spec.referenceLines ?? [];
  const bands = spec.referenceBands ?? [];
  if (lines.length === 0 && bands.length === 0) {
    return [];
  }
  if (spec.type === 'pie') {
    return ['reference lines and bands are not supported for pie'];
  }
  const log = Boolean(spec.yAxis?.log);
  const yLines = lines.filter((line) => line.axis === 'y').map((line) => line.value);
  const yBands = bands.filter((band) => band.axis === 'y').flatMap((band) => [band.from, band.to]);
  const errors = [
    ...valueMarkErrors('reference line values', yLines, log),
    ...valueMarkErrors('reference band bounds', yBands, log),
  ];
  const reversed = bands.some(
    (band) => typeof band.from === 'number' && typeof band.to === 'number' && band.from >= band.to,
  );
  if (reversed) {
    errors.push('reference band from must be less than to');
  }
  return errors;
}

/**
 * Flows summed per source/target pair, in first-seen order.  Rows
 * without both names or with a value that is not positive draw nothing.
 */
export function sankeyLinks(spec: ChartSpec, data: readonly DataRow[]): SankeyLink[] {
  const target = spec.target ?? '';
  const links = new Map<string, SankeyLink>();
  for (const row of data) {
    const value = toFiniteOrNull(row[spec.y[0]]);
    if (row[spec.x] == null || row[target] == null || value === null || value <= 0) {
      continue;
    }
    const link = { source: String(row[spec.x]), target: String(row[target]), value };
    const key = JSON.stringify([link.source, link.target]);
    const seen = links.get(key);
    links.set(key, seen ? { ...seen, value: seen.value + value } : link);
  }
  return [...links.values()];
}

function sankeyNodes(links: readonly SankeyLink[]): string[] {
  return [...new Set(links.flatMap((link) => [link.source, link.target]))];
}

/**
 * Nodes on a loop: peel off links that start at a node nothing flows
 * into or end at a node nothing flows out of until none are left to
 * peel.  ECharts lays out a sankey only when its flows never loop back.
 */
export function loopingNodes(links: readonly SankeyLink[]): string[] {
  let remaining = links;
  for (;;) {
    const targets = new Set(remaining.map((link) => link.target));
    const sources = new Set(remaining.map((link) => link.source));
    const kept = remaining.filter((link) => targets.has(link.source) && sources.has(link.target));
    if (kept.length === remaining.length) {
      return sankeyNodes(kept).sort();
    }
    remaining = kept;
  }
}

/** One pass pushing each target a column past its source; true when a column moved. */
function pushColumns(links: readonly SankeyLink[], columns: Map<string, number>): boolean {
  let moved = false;
  for (const link of links) {
    const next = (columns.get(link.source) ?? 0) + 1;
    if (next > (columns.get(link.target) ?? 0)) {
      columns.set(link.target, next);
      moved = true;
    }
  }
  return moved;
}

/**
 * Node count of the fullest column, placing each node by its longest
 * path from a start node.  Passes are capped at the node count, which
 * is enough without loops and still ends if a loop slipped through.
 */
export function sankeyBusiestColumn(links: readonly SankeyLink[]): number {
  const nodes = sankeyNodes(links);
  const columns = new Map(nodes.map((name) => [name, 0]));
  let pass = 0;
  while (pass < nodes.length && pushColumns(links, columns)) {
    pass += 1;
  }
  const counts = new Map<number, number>();
  for (const column of columns.values()) {
    counts.set(column, (counts.get(column) ?? 0) + 1);
  }
  return Math.max(0, ...counts.values());
}

/** Sankeys grow with their fullest column so every node keeps a readable label. */
export function chartHeightPx(spec: ChartSpec): number {
  if (spec.type !== 'sankey') {
    return CHART_HEIGHT_PX;
  }
  const busiest = sankeyBusiestColumn(sankeyLinks(spec, spec.data));
  return Math.max(CHART_HEIGHT_PX, busiest * SANKEY_PX_PER_NODE + SANKEY_VERTICAL_MARGIN_PX);
}

function sankeyFlowErrors(links: readonly SankeyLink[]): string[] {
  const errors: string[] = [];
  const nodeCount = sankeyNodes(links).length;
  if (nodeCount > MAX_SANKEY_NODES) {
    errors.push(`sankey has ${nodeCount} nodes; at most ${MAX_SANKEY_NODES}`);
  }
  const looping = loopingNodes(links);
  if (looping.length > 0) {
    errors.push(`sankey flows must not loop back (${looping.join(', ')})`);
  }
  return errors;
}

function sankeyErrors(spec: ChartSpec): string[] {
  if (spec.type !== 'sankey') {
    return spec.target !== undefined ? ['target requires type sankey'] : [];
  }
  if (spec.target === undefined) {
    return ['sankey requires target'];
  }
  const errors: string[] = [];
  if (spec.target === spec.x) {
    errors.push('target must differ from x');
  }
  if (spec.y.length !== 1) {
    errors.push('sankey takes exactly one y');
  }
  const unsupported = SANKEY_UNSUPPORTED.filter((key) => spec[key] !== undefined);
  if (unsupported.length > 0) {
    errors.push(`sankey does not support ${unsupported.join(', ')}`);
  }
  return errors.length > 0 ? errors : sankeyFlowErrors(sankeyLinks(spec, spec.data));
}

/**
 * Cross-field rules the flat schema cannot express.  Returns one
 * message per violated rule; an empty list means the spec is valid.
 * ``chart-spec.ts`` turns these into Zod issues.
 */
export function layoutErrors(spec: ChartSpec): string[] {
  return [
    ...layoutTypeErrors(spec),
    ...axisBoundErrors('yAxis', spec.yAxis),
    ...axisBoundErrors('y2Axis', spec.y2Axis),
    ...y2Errors(spec),
    ...referenceErrors(spec),
    ...sankeyErrors(spec),
  ];
}

function toFiniteOrNull(raw: unknown): number | null {
  // ``Number(null)`` and ``Number('')`` are 0; missing cells must stay gaps.
  if (raw === null || raw === '') {
    return null;
  }
  const value = typeof raw === 'number' ? raw : Number(raw);
  return Number.isFinite(value) ? value : null;
}

function definedOnly(entries: EChartsObject): EChartsObject {
  return Object.fromEntries(
    Object.entries(entries).filter(([, value]) => value !== undefined),
  );
}

/**
 * True when every row carries a finite number in ``field``.  Numeric
 * strings do not count: they come from category-like columns (years,
 * IDs) where a value axis would misrepresent the data.
 */
export function hasNumericX(data: readonly DataRow[], field: string): boolean {
  return data.length > 0
    && data.every((row) => {
      const raw = row[field];
      return typeof raw === 'number' && Number.isFinite(raw);
    });
}

function resolveXMode(spec: ChartSpec, type: ChartType, data: readonly DataRow[]): XMode {
  if (spec.xType === 'time') {
    return 'time';
  }
  // Scatter with numeric x needs a value axis so points sit at their
  // real x position; a category axis spaces them evenly in row order.
  return type === 'scatter' && hasNumericX(data, spec.x) ? 'value' : 'category';
}

/** Options the current (possibly switched) type does not support are dropped. */
function resolveLayout(spec: ChartSpec, type: ChartType, data: readonly DataRow[]): Layout {
  const xMode = resolveXMode(spec, type, data);
  const horizontal = Boolean(spec.horizontal) && HORIZONTAL_TYPES.includes(type) && xMode === 'category';
  const y2Fields = horizontal ? [] : spec.y2 ?? [];
  return {
    xMode,
    horizontal,
    stacked: Boolean(spec.stacked) && STACKABLE_TYPES.includes(type),
    zoom: data.length > DATA_ZOOM_THRESHOLD,
    multiSeries: spec.y.length + y2Fields.length > 1,
    y2Fields,
    sizeField: type === 'scatter' ? spec.size : undefined,
  };
}

/**
 * Map bubble values linearly onto bubble area between ``BUBBLE_MIN_PX``
 * and ``BUBBLE_MAX_PX`` diameter, so twice the value reads as twice the
 * ink.  Missing values get the smallest size; equal values the middle area.
 */
export function bubbleSizes(values: readonly (number | null)[]): number[] {
  const finite = values.filter((value): value is number => value !== null);
  const low = Math.min(...finite);
  const span = Math.max(...finite) - low;
  const minArea = BUBBLE_MIN_PX ** 2;
  const areaRange = BUBBLE_MAX_PX ** 2 - minArea;
  return values.map((value) => {
    if (value === null) {
      return BUBBLE_MIN_PX;
    }
    const share = span === 0 ? 0.5 : (value - low) / span;
    return Math.sqrt(minArea + share * areaRange);
  });
}

function axisName(name: string | undefined): EChartsObject {
  return name ? { name, nameTruncate: { maxWidth: AXIS_NAME_MAX_PX, ellipsis: '…' } } : {};
}

/** The name of the bottom axis sits centred below its labels, not beside the plot. */
function withBottomName(axis: EChartsObject): EChartsObject {
  return axis.name ? { ...axis, nameLocation: 'middle', nameGap: 30 } : axis;
}

/**
 * Short category axes show every label, cut to its share of the width;
 * ``hideOverlap`` still drops labels that collide on narrow screens.
 * Longer axes keep ECharts' automatic thinning.
 */
function categoryLabels(count: number, horizontal: boolean): EChartsObject | undefined {
  if (count > ALL_LABELS_MAX_CATEGORIES) {
    return undefined;
  }
  const share = Math.floor(CATEGORY_LABELS_BUDGET_PX / Math.max(count, 1));
  const width = horizontal ? CATEGORY_LABEL_MAX_PX : Math.min(CATEGORY_LABEL_MAX_PX, share);
  return { interval: 0, width, overflow: 'truncate', hideOverlap: true };
}

function buildXAxis(spec: ChartSpec, layout: Layout, data: readonly DataRow[]): EChartsObject {
  if (layout.xMode === 'time') {
    return definedOnly({ type: 'time', ...axisName(spec.xName) });
  }
  if (layout.xMode === 'value') {
    return definedOnly({ type: 'value', scale: true, boundaryGap: SCATTER_AXIS_PADDING, ...axisName(spec.xName) });
  }
  return definedOnly({
    type: 'category',
    data: data.map((row) => String(row[spec.x] ?? '')),
    ...axisName(spec.xName),
    axisLabel: categoryLabels(data.length, layout.horizontal),
    // Horizontal bars list categories top-down in row order, so a
    // top-N result reads from the largest value downwards.
    inverse: layout.horizontal ? true : undefined,
  });
}

function buildValueAxis(axis: ValueAxisSpec = {}, padded = false): EChartsObject {
  return definedOnly({
    type: axis.log ? 'log' : 'value',
    ...axisName(axis.name),
    min: axis.min,
    max: axis.max,
    boundaryGap: padded ? SCATTER_AXIS_PADDING : undefined,
  });
}

function buildPoints(field: string, context: SeriesContext): unknown[] {
  const { spec, layout, data, sizes } = context;
  return data.map((row, index) => {
    const y = toFiniteOrNull(row[field]);
    const value = layout.xMode === 'category' ? y : [row[spec.x], y];
    return sizes ? { value, symbolSize: sizes[index] } : value;
  });
}

function buildOneSeries(field: string, context: SeriesContext, axisIndex?: number): EChartsObject {
  const { type, layout, sizes } = context;
  return definedOnly({
    name: field,
    type: type === 'area' ? 'line' : type,
    data: buildPoints(field, context),
    yAxisIndex: axisIndex,
    stack: layout.stacked ? 'total' : undefined,
    areaStyle: type === 'area' ? {} : undefined,
    symbolSize: type === 'scatter' && !sizes ? 10 : undefined,
    itemStyle: type === 'scatter' ? { opacity: SCATTER_OPACITY } : undefined,
  });
}

/**
 * ``axis`` in a reference mark names the data axis (``x`` = category or
 * time, ``y`` = value), so horizontal bars swap the ECharts key.
 */
function markAxisKey(axis: ReferenceLine['axis'], horizontal: boolean): 'xAxis' | 'yAxis' {
  return (axis === 'x') !== horizontal ? 'xAxis' : 'yAxis';
}

function buildMarkLine(lines: readonly ReferenceLine[], horizontal: boolean): EChartsObject | undefined {
  if (lines.length === 0) {
    return undefined;
  }
  return {
    symbol: 'none',
    data: lines.map((line) => definedOnly({
      [markAxisKey(line.axis, horizontal)]: line.value,
      name: line.label,
      label: line.label ? { formatter: '{b}', position: 'insideEndTop' } : undefined,
    })),
  };
}

function buildMarkArea(bands: readonly ReferenceBand[], horizontal: boolean): EChartsObject | undefined {
  if (bands.length === 0) {
    return undefined;
  }
  return {
    data: bands.map((band) => {
      const key = markAxisKey(band.axis, horizontal);
      return [definedOnly({ [key]: band.from, name: band.label }), { [key]: band.to }];
    }),
  };
}

function buildSeries(context: SeriesContext): EChartsObject[] {
  const { spec, layout, data } = context;
  const { sizeField } = layout;
  const sizes = sizeField
    ? bubbleSizes(data.map((row) => toFiniteOrNull(row[sizeField])))
    : undefined;
  const sized = { ...context, sizes };
  const [first, ...rest] = spec.y.map((field) => buildOneSeries(field, sized));
  // Reference marks hang off the first left-axis series, so value
  // lines and bands are always measured against the left axis.
  const anchored = definedOnly({
    ...first,
    markLine: buildMarkLine(spec.referenceLines ?? [], layout.horizontal),
    markArea: buildMarkArea(spec.referenceBands ?? [], layout.horizontal),
  });
  const right = layout.y2Fields.map((field) => buildOneSeries(field, sized, 1));
  return [anchored, ...rest, ...right];
}

// A scrollable legend is required for multi-series charts: the
// default layout collapses to ``bottom: 0`` and wraps onto multiple
// rows, which silently overlaps the x-axis and visually swallows the
// bars.  ``type: 'scroll'`` keeps the legend on a single row with
// arrows; ``grid.bottom`` is raised to leave room for it.
//
// The margins reserve room for title, legend and zoom only.  Outer
// bounds ``same``/``all`` make ECharts shrink the plot until axis labels
// and axis names fit inside them, whatever their length; the legacy
// ``containLabel`` covers labels but lets names spill off the canvas.
function buildGrid(spec: ChartSpec, layout: Layout): EChartsObject {
  const bottomZoom = layout.zoom && !layout.horizontal;
  const sideZoom = layout.zoom && layout.horizontal;
  return {
    left: 48,
    right: 16 + (sideZoom ? 32 : 0),
    top: spec.title ? 48 : 16,
    bottom: (layout.multiSeries ? 56 : 48) + (bottomZoom ? 32 : 0),
    outerBoundsMode: 'same',
    outerBoundsContain: 'all',
  };
}

// Slider only: an ``inside`` zoom would capture the mouse wheel and
// stop the chat from scrolling while the pointer is over a chart.
function buildDataZoom(layout: Layout): EChartsObject[] | undefined {
  if (!layout.zoom) {
    return undefined;
  }
  if (layout.horizontal) {
    return [{ type: 'slider', yAxisIndex: 0, right: 8 }];
  }
  return [{ type: 'slider', xAxisIndex: 0, bottom: layout.multiSeries ? 28 : 8 }];
}

function buildTitle(title: string | undefined): EChartsObject | undefined {
  if (!title) {
    return undefined;
  }
  const text = title.length > TITLE_MAX_CHARS ? `${title.slice(0, TITLE_MAX_CHARS - 1).trimEnd()}…` : title;
  return { text, left: 'center' };
}

function buildPieOption(spec: ChartSpec, data: readonly DataRow[]): EChartsObject {
  const series = spec.y[0];
  return {
    title: buildTitle(spec.title),
    tooltip: { trigger: 'item' },
    legend: { bottom: 0, type: 'scroll' },
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

// Labels sit to the right of each node, so the right margin keeps the
// last column's labels on the canvas.
function buildSankeyOption(spec: ChartSpec, data: readonly DataRow[]): EChartsObject {
  const links = sankeyLinks(spec, data);
  return {
    title: buildTitle(spec.title),
    tooltip: { trigger: 'item' },
    series: [
      {
        type: 'sankey',
        top: spec.title ? 48 : 16,
        bottom: 16,
        left: 16,
        right: SANKEY_LABEL_MAX_PX + 8,
        data: sankeyNodes(links).map((name) => ({ name })),
        links,
        nodeGap: SANKEY_NODE_GAP_PX,
        emphasis: { focus: 'adjacency' },
        label: { width: SANKEY_LABEL_MAX_PX, overflow: 'truncate' },
        labelLayout: { hideOverlap: true },
        lineStyle: { color: 'gradient', curveness: 0.5 },
      },
    ],
    color: spec.palette ?? DEFAULT_PALETTE,
  };
}

function resolveType(spec: ChartSpec, typeOverride: ChartType | undefined): ChartType {
  const switchable = typeOverride !== undefined
    && SWITCHABLE_TYPES.includes(spec.type)
    && SWITCHABLE_TYPES.includes(typeOverride);
  return switchable ? typeOverride : spec.type;
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
}: BuildOptionInput): EChartsObject {
  const effectiveType = resolveType(spec, typeOverride);
  const data = drillFilter != null
    ? spec.data.filter((row) => String(row[spec.x]) === drillFilter)
    : spec.data;

  if (effectiveType === 'pie') {
    return buildPieOption(spec, data);
  }
  if (effectiveType === 'sankey') {
    return buildSankeyOption(spec, data);
  }

  const layout = resolveLayout(spec, effectiveType, data);
  const xAxis = buildXAxis(spec, layout, data);
  const padded = effectiveType === 'scatter';
  const valueAxis = buildValueAxis(spec.yAxis, padded);
  const valueAxes = layout.y2Fields.length > 0 ? [valueAxis, buildValueAxis(spec.y2Axis, padded)] : valueAxis;
  return {
    title: buildTitle(spec.title),
    tooltip: { trigger: layout.xMode === 'value' ? 'item' : 'axis' },
    legend: layout.multiSeries ? { bottom: 0, type: 'scroll' } : undefined,
    grid: buildGrid(spec, layout),
    xAxis: withBottomName(layout.horizontal ? valueAxis : xAxis),
    yAxis: layout.horizontal ? xAxis : valueAxes,
    dataZoom: buildDataZoom(layout),
    series: buildSeries({ spec, type: effectiveType, layout, data }),
    color: spec.palette ?? DEFAULT_PALETTE,
  };
}
