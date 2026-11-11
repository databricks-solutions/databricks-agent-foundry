/**
 * Read-only ECharts renderer for the ``echarts-chart`` fence.
 *
 * Heavy ECharts imports live here so the fence-dispatcher in
 * ``response.tsx`` can ``lazy()``-load the whole module — initial page
 * loads are unaffected by ECharts' bundle size until the user actually
 * receives a chart.
 *
 * Design adapted from
 * https://github.com/databricks-solutions/dbx-unifiedchat
 * at commit e1e1732b1c0a5b14b0208898ebaab71119164498.  Customizer /
 * NL-rechart features are deliberately omitted (see issue #105
 * "Out of scope").
 */
import { useCallback, useMemo, useState } from 'react';
import ReactECharts from 'echarts-for-react';
import { Download, RotateCcw } from 'lucide-react';

import {
  CHART_TYPES,
  type ChartSpec,
  type ChartType,
  buildEChartsOption,
  serializeToCsv,
} from './chart-spec';
import { downloadTextFile } from './message-context';

const CHART_HEIGHT_PX = 320;
// Canvas renderer chosen over SVG: ECharts' SVG path leaves stale ``<g>``
// elements around when ``notMerge`` swaps the chart type at runtime
// (bar -> line/area, etc.), which manifests as bars/lines silently
// disappearing while the data and tooltip continue to work.  Canvas
// redraws the whole frame each time and avoids the stale-DOM issue.
const CHART_RENDERER_OPTS = { renderer: 'canvas' as const };

interface InteractiveChartProps {
  spec: ChartSpec;
}

export function InteractiveChart({ spec }: InteractiveChartProps) {
  const [typeOverride, setTypeOverride] = useState<ChartType>(spec.type);
  const [drillFilter, setDrillFilter] = useState<string | null>(null);

  const option = useMemo(
    () => buildEChartsOption({ spec, typeOverride, drillFilter }),
    [spec, typeOverride, drillFilter],
  );

  const handleDownloadCsv = useCallback(() => {
    const filename = `${(spec.title ?? 'chart').replace(/\W+/g, '-')}.csv`;
    downloadTextFile(filename, serializeToCsv(spec), 'text/csv;charset=utf-8');
  }, [spec]);

  const handleReset = useCallback(() => {
    setTypeOverride(spec.type);
    setDrillFilter(null);
  }, [spec.type]);

  const onEvents = useMemo(
    () => ({
      click: (params: { name?: string }) => {
        if (params?.name && typeOverride !== 'pie') {
          setDrillFilter((prev) =>
            prev === params.name ? null : (params.name ?? null),
          );
        }
      },
    }),
    [typeOverride],
  );

  const isReset = typeOverride === spec.type && drillFilter === null;

  return (
    // ``min-w-0`` is required so the chart stops shrinking to fit the
    // narrowest content of its flex parent (Streamdown chat bubbles
    // default to a constrained ``max-w`` that collapses the chart
    // canvas down to ~300px, which renders bars at sub-pixel widths
    // and series colors swatch to ``transparent``).  ``w-full`` then
    // lets the chart claim the full available width of its bubble.
    <div className="my-2 w-full min-w-0 rounded-md border border-border bg-background">
      <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-1.5">
        <div className="flex items-center gap-2">
          <label className="text-xs text-muted-foreground">
            Type
            <select
              value={typeOverride}
              onChange={(e) => setTypeOverride(e.target.value as ChartType)}
              className="ml-1 rounded border border-border bg-background px-1 py-0.5 text-xs"
            >
              {CHART_TYPES.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          {drillFilter && (
            <span className="rounded bg-muted px-1.5 py-0.5 text-xs">
              filter: {drillFilter}
            </span>
          )}
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={handleReset}
            disabled={isReset}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs hover:bg-muted disabled:opacity-40"
            aria-label="Reset"
          >
            <RotateCcw className="h-3.5 w-3.5" />
            Reset
          </button>
          <button
            type="button"
            onClick={handleDownloadCsv}
            className="inline-flex items-center gap-1 rounded px-2 py-1 text-xs hover:bg-muted"
            aria-label="Download CSV"
          >
            <Download className="h-3.5 w-3.5" />
            CSV
          </button>
        </div>
      </div>
      <div className="px-2 py-2">
        {/*
          Do NOT pass ``lazyUpdate``: it triggers apache/echarts#14846
          when combined with any post-init ``resize()`` (which
          ``echarts-for-react`` performs internally on container
          resize via size-sensor) and leaves the chart with blank
          series even though axes/tooltip/legend render.  ``notMerge``
          is enough for our use case because we always pass a fresh
          option object, never a partial.
        */}
        <ReactECharts
          option={option}
          opts={CHART_RENDERER_OPTS}
          style={{ height: CHART_HEIGHT_PX, width: '100%' }}
          notMerge
          onEvents={onEvents}
        />
      </div>
    </div>
  );
}

export default InteractiveChart;
