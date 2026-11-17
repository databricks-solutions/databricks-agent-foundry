import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  ALL_LABELS_MAX_CATEGORIES,
  AXIS_NAME_MAX_PX,
  BUBBLE_MAX_PX,
  BUBBLE_MIN_PX,
  bubbleSizes,
  buildEChartsOption,
  CHART_HEIGHT_PX,
  chartHeightPx,
  DATA_ZOOM_THRESHOLD,
  DEFAULT_PALETTE,
  hasNumericX,
  layoutErrors,
  loopingNodes,
  MAX_SANKEY_NODES,
  sankeyBusiestColumn,
  sankeyLinks,
  TITLE_MAX_CHARS,
} from '../../overrides/client/src/components/elements/chart-option.ts';
import type { ChartSpec } from '../../overrides/client/src/components/elements/chart-spec.ts';

type Option = Record<string, any>;

const categorySpec: ChartSpec = {
  type: 'bar',
  x: 'q',
  y: ['rev', 'cost'],
  data: [
    { q: 'Q1', rev: 120, cost: '80' },
    { q: 'Q2', rev: 150, cost: 'n/a' },
  ],
};

const numericScatterSpec: ChartSpec = {
  type: 'scatter',
  x: 'size',
  y: ['price'],
  data: [
    { size: 10, price: 100 },
    { size: 250, price: 900 },
  ],
};

const nameTruncate = { maxWidth: AXIS_NAME_MAX_PX, ellipsis: '…' };
const twoLabels = { interval: 0, width: 160, overflow: 'truncate', hideOverlap: true };

function build(spec: ChartSpec, extra: Partial<Parameters<typeof buildEChartsOption>[0]> = {}): Option {
  return buildEChartsOption({ spec, ...extra });
}

describe('hasNumericX', () => {
  it('is true when every x value is a finite number', () => {
    assert.equal(hasNumericX(numericScatterSpec.data, 'size'), true);
  });

  it('is false for numeric strings, non-finite numbers and empty data', () => {
    assert.equal(hasNumericX([{ x: '2024' }], 'x'), false);
    assert.equal(hasNumericX([{ x: 1 }, { x: Number.NaN }], 'x'), false);
    assert.equal(hasNumericX([], 'x'), false);
  });
});

describe('buildEChartsOption', () => {
  it('renders category charts with one series per y field', () => {
    const option = build(categorySpec);
    assert.deepEqual(option.xAxis, { type: 'category', data: ['Q1', 'Q2'], axisLabel: twoLabels });
    assert.deepEqual(option.series.map((s: Option) => s.name), ['rev', 'cost']);
    assert.deepEqual(option.series[1].data, [80, null]);
    assert.equal(option.legend.type, 'scroll');
    assert.deepEqual(option.color, DEFAULT_PALETTE);
  });

  it('places numeric scatter points on a value axis as [x, y] pairs', () => {
    const option = build(numericScatterSpec);
    assert.equal(option.xAxis.type, 'value');
    assert.equal(option.tooltip.trigger, 'item');
    assert.deepEqual(option.series[0].data, [[10, 100], [250, 900]]);
  });

  it('keeps a category axis when scatter x is not numeric', () => {
    const option = build(categorySpec, { typeOverride: 'scatter' });
    assert.equal(option.xAxis.type, 'category');
    assert.deepEqual(option.series[0].data, [120, 150]);
  });

  it('keeps a category axis for line charts with numeric x', () => {
    const option = build(numericScatterSpec, { typeOverride: 'line' });
    assert.deepEqual(option.xAxis, { type: 'category', data: ['10', '250'], axisLabel: twoLabels });
  });

  it('keeps null and empty cells as gaps instead of zero', () => {
    const spec: ChartSpec = { type: 'line', x: 'q', y: ['rev'], data: [{ q: 'Q1', rev: null }, { q: 'Q2', rev: '' }] };
    assert.deepEqual(build(spec).series[0].data, [null, null]);
  });

  it('maps area to a line series with areaStyle', () => {
    const option = build(categorySpec, { typeOverride: 'area' });
    assert.equal(option.series[0].type, 'line');
    assert.deepEqual(option.series[0].areaStyle, {});
  });

  it('renders pie from the first y field only', () => {
    const option = build(categorySpec, { typeOverride: 'pie' });
    assert.deepEqual(option.series[0].data, [
      { name: 'Q1', value: 120 },
      { name: 'Q2', value: 150 },
    ]);
  });

  it('keeps a pie legend on one scrollable row', () => {
    const option = build(categorySpec, { typeOverride: 'pie' });
    assert.deepEqual(option.legend, { bottom: 0, type: 'scroll' });
  });

  it('filters rows to the drilled-in x value', () => {
    const option = build(categorySpec, { drillFilter: 'Q2' });
    assert.deepEqual(option.xAxis.data, ['Q2']);
  });

  it('uses the spec palette when provided', () => {
    const option = build({ ...categorySpec, palette: ['#000'] });
    assert.deepEqual(option.color, ['#000']);
  });
});

const timeSpec: ChartSpec = {
  type: 'line',
  x: 'day',
  y: ['count'],
  xType: 'time',
  data: [
    { day: '2026-05-01', count: 3 },
    { day: '2026-05-04', count: 7 },
  ],
};

function manyRows(count: number): ChartSpec['data'] {
  return Array.from({ length: count }, (_, i) => ({ q: `c${i}`, rev: i, cost: i }));
}

describe('layoutErrors', () => {
  it('accepts a spec without layout options', () => {
    assert.deepEqual(layoutErrors(categorySpec), []);
  });

  it('rejects options the chart type does not support', () => {
    assert.deepEqual(layoutErrors({ ...categorySpec, type: 'line', horizontal: true, stacked: true }), [
      'horizontal requires type bar',
      'stacked requires type bar or area',
    ]);
    assert.deepEqual(layoutErrors({ ...categorySpec, type: 'pie', xType: 'time' }), [
      'xType time is not supported for pie',
    ]);
  });

  it('rejects stacking on a log y axis', () => {
    assert.deepEqual(layoutErrors({ ...categorySpec, stacked: true, yAxis: { log: true } }), [
      'stacked does not support a log y axis',
    ]);
  });

  it('rejects horizontal bars on a time axis', () => {
    assert.deepEqual(layoutErrors({ ...categorySpec, horizontal: true, xType: 'time' }), [
      'horizontal does not support xType time',
    ]);
  });

  it('rejects inconsistent y-axis bounds', () => {
    assert.deepEqual(layoutErrors({ ...categorySpec, yAxis: { log: true, min: 0 } }), [
      'yAxis.min must be greater than 0 on a log axis',
    ]);
    assert.deepEqual(layoutErrors({ ...categorySpec, yAxis: { min: 5, max: 5 } }), [
      'yAxis.min must be less than yAxis.max',
    ]);
  });
});

describe('buildEChartsOption axes', () => {
  it('places time-axis points as [date, value] pairs', () => {
    const option = build(timeSpec);
    assert.deepEqual(option.xAxis, { type: 'time' });
    assert.equal(option.tooltip.trigger, 'axis');
    assert.deepEqual(option.series[0].data, [['2026-05-01', 3], ['2026-05-04', 7]]);
  });

  it('applies axis names and y-axis log, min and max', () => {
    const option = build({
      ...categorySpec,
      xName: 'Quarter',
      yAxis: { name: 'EUR', log: true, min: 1, max: 1000 },
    });
    assert.equal(option.xAxis.name, 'Quarter');
    assert.deepEqual(option.yAxis, { type: 'log', name: 'EUR', nameTruncate, min: 1, max: 1000 });
  });

  it('centres the bottom axis name below the labels and caps every name', () => {
    const option = build({ ...categorySpec, xName: 'Quarter', yAxis: { name: 'EUR' } });
    assert.equal(option.xAxis.nameLocation, 'middle');
    assert.deepEqual(option.xAxis.nameTruncate, nameTruncate);
    assert.equal(option.yAxis.nameLocation, undefined);
  });

  it('shows every label on short category axes and thins out longer ones', () => {
    const rows = (count: number) => Array.from({ length: count }, (_, i) => ({ q: `Q${i}`, rev: i, cost: i }));
    const short = build({ ...categorySpec, data: rows(5) }).xAxis.axisLabel;
    assert.deepEqual(short, { interval: 0, width: 120, overflow: 'truncate', hideOverlap: true });
    assert.equal(build({ ...categorySpec, data: rows(ALL_LABELS_MAX_CATEGORIES + 1) }).xAxis.axisLabel, undefined);
  });

  it('keeps axis names and labels inside the grid margins', () => {
    const plain = build(categorySpec).grid;
    const named = build({ ...categorySpec, xName: 'Average trip distance (miles)', yAxis: { name: 'EUR' } }).grid;
    assert.deepEqual(named, plain);
    assert.equal(named.outerBoundsMode, 'same');
    assert.equal(named.outerBoundsContain, 'all');
    assert.equal(named.containLabel, undefined);
  });
});

describe('buildEChartsOption layout', () => {
  it('stacks every series when stacked is set', () => {
    const option = build({ ...categorySpec, stacked: true });
    assert.deepEqual(option.series.map((s: Option) => s.stack), ['total', 'total']);
  });

  it('drops stacking when the toolbar switches to a type without it', () => {
    const option = build({ ...categorySpec, stacked: true }, { typeOverride: 'line' });
    assert.equal(option.series[0].stack, undefined);
  });

  it('swaps the axes for horizontal bars and lists categories top-down', () => {
    const option = build({ ...categorySpec, horizontal: true, yAxis: { name: 'EUR' } });
    assert.deepEqual(option.yAxis, { type: 'category', data: ['Q1', 'Q2'], axisLabel: twoLabels, inverse: true });
    assert.deepEqual(option.xAxis, { type: 'value', name: 'EUR', nameTruncate, nameLocation: 'middle', nameGap: 30 });
  });

  it('drops horizontal when the toolbar switches away from bar', () => {
    const option = build({ ...categorySpec, horizontal: true }, { typeOverride: 'line' });
    assert.equal(option.xAxis.type, 'category');
  });
});

describe('buildEChartsOption zoom', () => {
  it('adds no zoom slider at or below the threshold', () => {
    const option = build({ ...categorySpec, data: manyRows(DATA_ZOOM_THRESHOLD) });
    assert.equal(option.dataZoom, undefined);
  });

  it('adds an x-axis zoom slider above the threshold', () => {
    const option = build({ ...categorySpec, data: manyRows(DATA_ZOOM_THRESHOLD + 1) });
    assert.equal(option.dataZoom.length, 1);
    assert.equal(option.dataZoom[0].type, 'slider');
    assert.equal(option.dataZoom[0].xAxisIndex, 0);
  });

  it('puts the zoom slider on the category axis of horizontal bars', () => {
    const option = build({ ...categorySpec, horizontal: true, data: manyRows(DATA_ZOOM_THRESHOLD + 1) });
    assert.equal(option.dataZoom[0].yAxisIndex, 0);
  });

  it('ignores reference marks and y2 when switched to pie', () => {
    const spec: ChartSpec = {
      ...categorySpec,
      y: ['rev'],
      y2: ['cost'],
      referenceLines: [{ axis: 'y', value: 100 }],
    };
    const option = build(spec, { typeOverride: 'pie' });
    assert.equal(option.series.length, 1);
    assert.equal(option.series[0].markLine, undefined);
  });

  it('ignores axis and layout options when switched to pie', () => {
    const spec = { ...categorySpec, stacked: true, xName: 'Quarter', data: manyRows(DATA_ZOOM_THRESHOLD + 1) };
    const option = build(spec, { typeOverride: 'pie' });
    assert.equal(option.xAxis, undefined);
    assert.equal(option.dataZoom, undefined);
  });
});

const dualAxisSpec: ChartSpec = {
  type: 'line',
  x: 'q',
  y: ['rev'],
  y2: ['margin'],
  y2Axis: { name: '%', min: 0, max: 100 },
  data: [
    { q: 'Q1', rev: 120, margin: 31 },
    { q: 'Q2', rev: 150, margin: 28 },
  ],
};

const bubbleSpec: ChartSpec = {
  type: 'scatter',
  x: 'risk',
  y: ['return'],
  size: 'aum',
  data: [
    { risk: 1, return: 2, aum: 10 },
    { risk: 2, return: 4, aum: 30 },
    { risk: 3, return: 5, aum: null },
  ],
};

describe('layoutErrors for y2, size and reference marks', () => {
  it('accepts a valid dual-axis spec', () => {
    assert.deepEqual(layoutErrors(dualAxisSpec), []);
  });

  it('rejects y2 with pie, horizontal or stacked', () => {
    assert.deepEqual(layoutErrors({ ...dualAxisSpec, type: 'pie' }), ['y2 is not supported for pie']);
    assert.deepEqual(layoutErrors({ ...dualAxisSpec, type: 'bar', horizontal: true }), [
      'y2 does not support horizontal',
    ]);
    assert.deepEqual(layoutErrors({ ...dualAxisSpec, type: 'bar', stacked: true }), [
      'y2 does not support stacked',
    ]);
  });

  it('rejects y2 fields repeated from y and y2Axis without y2', () => {
    assert.deepEqual(layoutErrors({ ...dualAxisSpec, y2: ['rev'] }), ['y2 fields must not repeat y fields']);
    assert.deepEqual(layoutErrors({ ...categorySpec, y2Axis: { name: '%' } }), ['y2Axis requires y2']);
  });

  it('applies the y-axis bound rules to y2Axis', () => {
    assert.deepEqual(layoutErrors({ ...dualAxisSpec, y2Axis: { log: true, min: 0 } }), [
      'y2Axis.min must be greater than 0 on a log axis',
    ]);
  });

  it('rejects size outside scatter', () => {
    assert.deepEqual(layoutErrors({ ...bubbleSpec, type: 'line' }), ['size requires type scatter']);
  });

  it('rejects reference marks on pie', () => {
    assert.deepEqual(layoutErrors({ ...categorySpec, type: 'pie', referenceLines: [{ axis: 'x', value: 'Q1' }] }), [
      'reference lines and bands are not supported for pie',
    ]);
  });

  it('requires numeric, log-safe values for y-axis marks', () => {
    assert.deepEqual(layoutErrors({ ...categorySpec, referenceLines: [{ axis: 'y', value: 'high' }] }), [
      'reference line values on the y axis must be numeric',
    ]);
    const logSpec: ChartSpec = { ...categorySpec, yAxis: { log: true }, referenceBands: [{ axis: 'y', from: 0, to: 5 }] };
    assert.deepEqual(layoutErrors(logSpec), [
      'reference band bounds on the y axis must be greater than 0 on a log axis',
    ]);
  });

  it('allows category values on x-axis marks but rejects reversed numeric bands', () => {
    assert.deepEqual(layoutErrors({ ...categorySpec, referenceBands: [{ axis: 'x', from: 'Q1', to: 'Q2' }] }), []);
    assert.deepEqual(layoutErrors({ ...categorySpec, referenceBands: [{ axis: 'y', from: 9, to: 3 }] }), [
      'reference band from must be less than to',
    ]);
  });
});

describe('buildEChartsOption second y axis', () => {
  it('adds a right-hand value axis and binds y2 series to it', () => {
    const option = build(dualAxisSpec);
    assert.deepEqual(option.yAxis, [{ type: 'value' }, { type: 'value', name: '%', nameTruncate, min: 0, max: 100 }]);
    assert.deepEqual(option.series.map((s: Option) => [s.name, s.yAxisIndex]), [['rev', undefined], ['margin', 1]]);
    assert.equal(option.legend.type, 'scroll');
  });

  it('includes y2 series in the toolbar type switch', () => {
    const option = build(dualAxisSpec, { typeOverride: 'bar' });
    assert.deepEqual(option.series.map((s: Option) => s.type), ['bar', 'bar']);
  });
});

describe('bubbleSizes', () => {
  const middle = Math.sqrt((BUBBLE_MIN_PX ** 2 + BUBBLE_MAX_PX ** 2) / 2);

  it('maps values linearly onto bubble area', () => {
    assert.deepEqual(bubbleSizes([10, 20, 30]), [BUBBLE_MIN_PX, middle, BUBBLE_MAX_PX]);
  });

  it('gives missing values the smallest and equal values the middle area', () => {
    assert.deepEqual(bubbleSizes([null, 5, 5]), [BUBBLE_MIN_PX, middle, middle]);
    assert.deepEqual(bubbleSizes([null]), [BUBBLE_MIN_PX]);
  });
});

describe('buildEChartsOption bubble charts', () => {
  it('sizes each scatter point from the size field', () => {
    const option = build(bubbleSpec);
    assert.deepEqual(option.series[0].data, [
      { value: [1, 2], symbolSize: BUBBLE_MIN_PX },
      { value: [2, 4], symbolSize: BUBBLE_MAX_PX },
      { value: [3, 5], symbolSize: BUBBLE_MIN_PX },
    ]);
    assert.equal(option.series[0].symbolSize, undefined);
  });

  it('draws points see-through and pads both value axes', () => {
    const option = build(bubbleSpec);
    assert.deepEqual(option.series[0].itemStyle, { opacity: 0.7 });
    assert.deepEqual(option.xAxis.boundaryGap, ['5%', '5%']);
    assert.deepEqual(option.yAxis.boundaryGap, ['5%', '5%']);
  });

  it('drops bubble sizing, transparency and padding when switched away from scatter', () => {
    const option = build(bubbleSpec, { typeOverride: 'line' });
    assert.deepEqual(option.series[0].data, [2, 4, 5]);
    assert.equal(option.series[0].itemStyle, undefined);
    assert.equal(option.yAxis.boundaryGap, undefined);
  });
});

describe('buildEChartsOption title', () => {
  it('keeps a short title and centres it', () => {
    assert.deepEqual(build({ ...categorySpec, title: 'Revenue' }).title, { text: 'Revenue', left: 'center' });
    assert.equal(build(categorySpec).title, undefined);
  });

  it('ends a long title in an ellipsis, on pie too', () => {
    const long = 'x'.repeat(TITLE_MAX_CHARS + 30);
    for (const typeOverride of ['bar', 'pie'] as const) {
      const { text } = build({ ...categorySpec, title: long }, { typeOverride }).title;
      assert.equal(text.length, TITLE_MAX_CHARS);
      assert.ok(text.endsWith('…'));
    }
  });

  it('leaves a title of exactly the limit alone', () => {
    const exact = 'x'.repeat(TITLE_MAX_CHARS);
    assert.equal(build({ ...categorySpec, title: exact }).title.text, exact);
  });
});

describe('buildEChartsOption reference marks', () => {
  const marked: ChartSpec = {
    ...categorySpec,
    referenceLines: [{ axis: 'y', value: 130, label: 'Target' }, { axis: 'x', value: 'Q2' }],
    referenceBands: [{ axis: 'x', from: 'Q1', to: 'Q2', label: 'Promo' }],
  };

  it('attaches lines and bands to the first series only', () => {
    const option = build(marked);
    assert.deepEqual(option.series[0].markLine, {
      symbol: 'none',
      data: [
        { yAxis: 130, name: 'Target', label: { formatter: '{b}', position: 'insideEndTop' } },
        { xAxis: 'Q2' },
      ],
    });
    assert.deepEqual(option.series[0].markArea, { data: [[{ xAxis: 'Q1', name: 'Promo' }, { xAxis: 'Q2' }]] });
    assert.equal(option.series[1].markLine, undefined);
  });

  it('swaps mark keys for horizontal bars', () => {
    const option = build({ ...marked, horizontal: true });
    assert.deepEqual(option.series[0].markLine.data.map((d: Option) => Object.keys(d)[0]), ['xAxis', 'yAxis']);
    assert.equal(Object.keys(option.series[0].markArea.data[0][0])[0], 'yAxis');
  });

  it('omits mark entries when the spec has none', () => {
    const option = build(categorySpec);
    assert.equal('markLine' in option.series[0], false);
    assert.equal('markArea' in option.series[0], false);
  });
});

describe('sankey charts', () => {
  const flowSpec: ChartSpec = {
    type: 'sankey',
    x: 'from',
    target: 'to',
    y: ['amount'],
    data: [
      { from: 'Budget', to: 'Compute', amount: 60 },
      { from: 'Budget', to: 'Storage', amount: '30' },
      { from: 'Compute', to: 'Jobs', amount: 40 },
      { from: 'Budget', to: 'Compute', amount: 5 },
      { from: 'Budget', to: 'Unused', amount: 0 },
      { from: null, to: 'Jobs', amount: 3 },
    ],
  };

  it('sums repeated flows and drops rows that draw nothing', () => {
    assert.deepEqual(sankeyLinks(flowSpec, flowSpec.data), [
      { source: 'Budget', target: 'Compute', value: 65 },
      { source: 'Budget', target: 'Storage', value: 30 },
      { source: 'Compute', target: 'Jobs', value: 40 },
    ]);
  });

  it('builds one sankey series from the nodes and flows, without axes', () => {
    const option = build({ ...flowSpec, title: 'Spend' });
    assert.equal(option.xAxis, undefined);
    assert.equal(option.series.length, 1);
    assert.equal(option.series[0].type, 'sankey');
    assert.deepEqual(option.series[0].data, [
      { name: 'Budget' },
      { name: 'Compute' },
      { name: 'Storage' },
      { name: 'Jobs' },
    ]);
    assert.equal(option.series[0].links.length, 3);
    assert.equal(option.title.text, 'Spend');
  });

  it('ignores toolbar type switches to and from sankey', () => {
    assert.equal(build(flowSpec, { typeOverride: 'bar' }).series[0].type, 'sankey');
    assert.equal(build(categorySpec, { typeOverride: 'sankey' }).series[0].type, 'bar');
  });

  it('accepts a valid flow spec', () => {
    assert.deepEqual(layoutErrors(flowSpec), []);
  });

  it('requires target on sankey and only there', () => {
    const { target: _, ...withoutTarget } = flowSpec;
    assert.deepEqual(layoutErrors(withoutTarget), ['sankey requires target']);
    assert.deepEqual(layoutErrors({ ...categorySpec, target: 'cost' }), ['target requires type sankey']);
  });

  it('rejects a second y, target equal to x and axis options', () => {
    assert.deepEqual(layoutErrors({ ...flowSpec, y: ['amount', 'other'], target: 'from', xName: 'Source' }), [
      'target must differ from x',
      'sankey takes exactly one y',
      'sankey does not support xName',
    ]);
  });

  it('names the nodes on a loop', () => {
    const looped = [
      { source: 'A', target: 'B', value: 1 },
      { source: 'B', target: 'C', value: 1 },
      { source: 'C', target: 'A', value: 1 },
      { source: 'Start', target: 'A', value: 1 },
      { source: 'C', target: 'End', value: 1 },
    ];
    assert.deepEqual(loopingNodes(looped), ['A', 'B', 'C']);
    assert.deepEqual(loopingNodes([{ source: 'A', target: 'A', value: 1 }]), ['A']);
    assert.deepEqual(loopingNodes(sankeyLinks(flowSpec, flowSpec.data)), []);
    const spec = { ...flowSpec, data: [...flowSpec.data, { from: 'Jobs', to: 'Budget', amount: 1 }] };
    assert.deepEqual(layoutErrors(spec), ['sankey flows must not loop back (Budget, Compute, Jobs)']);
  });

  it('spaces nodes one label line apart and hides labels that still collide', () => {
    const series = build(flowSpec).series[0];
    assert.equal(series.nodeGap, 14);
    assert.deepEqual(series.labelLayout, { hideOverlap: true });
  });

  it('counts nodes in the fullest column by longest path', () => {
    const links = [
      { source: 'A', target: 'B', value: 1 },
      { source: 'B', target: 'C', value: 1 },
      { source: 'A', target: 'C', value: 1 },
      { source: 'D', target: 'C', value: 1 },
    ];
    assert.equal(sankeyBusiestColumn(links), 2);
    assert.equal(sankeyBusiestColumn([]), 0);
  });

  it('grows a busy sankey taller and keeps the default height otherwise', () => {
    assert.equal(chartHeightPx(categorySpec), CHART_HEIGHT_PX);
    assert.equal(chartHeightPx(flowSpec), CHART_HEIGHT_PX);
    const data = Array.from({ length: 20 }, (_, i) => ({ from: `Pickup ${i}`, to: `Dropoff ${i}`, amount: 1 }));
    assert.equal(chartHeightPx({ ...flowSpec, data }), 20 * 22 + 64);
  });

  it('caps the number of nodes', () => {
    const data = Array.from({ length: MAX_SANKEY_NODES }, (_, i) => ({ from: 'Root', to: `Leaf ${i}`, amount: 1 }));
    assert.deepEqual(layoutErrors({ ...flowSpec, data }), [
      `sankey has ${MAX_SANKEY_NODES + 1} nodes; at most ${MAX_SANKEY_NODES}`,
    ]);
  });
});
