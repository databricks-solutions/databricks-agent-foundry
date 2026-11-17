import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import type { ChatMessage } from '@chat-template/core';
import {
  chartPlaceholder,
  omitChartData,
} from '../../overrides/server/src/lib/chart-fence-placeholder.ts';

function makeMessage(role: string, parts: unknown[]): ChatMessage {
  return { id: 'm1', role, parts } as unknown as ChatMessage;
}

function partsOf(message: ChatMessage): any[] {
  return (message as unknown as { parts: any[] }).parts;
}

const SPEC = { type: 'line', title: 'DBUs per month', x: 'month', y: ['dbus'], data: [{ month: '2026-01', dbus: 1 }, { month: '2026-02', dbus: 2 }] };
const FENCE = `\`\`\`echarts-chart\n${JSON.stringify(SPEC)}\n\`\`\``;

describe('chartPlaceholder', () => {
  it('names the type, title and row count', () => {
    assert.equal(
      chartPlaceholder(JSON.stringify(SPEC)),
      '[An interactive chart was shown here (line, "DBUs per month", 2 rows). Its data is left out of this history.]',
    );
  });

  it('falls back to a generic placeholder for a body that is not JSON', () => {
    assert.equal(chartPlaceholder('type: bar\n---\nq,rev'), '[An interactive chart was shown here. Its data is left out of this history.]');
  });

  it('caps long titles', () => {
    const placeholder = chartPlaceholder(JSON.stringify({ ...SPEC, title: 'x'.repeat(200) }));
    assert.ok(placeholder.includes(`"${'x'.repeat(80)}"`));
    assert.ok(!placeholder.includes('x'.repeat(81)));
  });
});

describe('omitChartData', () => {
  it('replaces a chart fence in assistant text and keeps the surrounding prose', () => {
    const [out] = omitChartData([makeMessage('assistant', [{ type: 'text', text: `Here is the trend:\n\n${FENCE}\n\nUsage doubled.` }])]);
    const text = partsOf(out)[0].text;
    assert.ok(text.startsWith('Here is the trend:\n\n[An interactive chart was shown here (line'));
    assert.ok(text.endsWith('\n\nUsage doubled.'));
    assert.ok(!text.includes('2026-01'));
  });

  it('replaces every fence in a part', () => {
    const [out] = omitChartData([makeMessage('assistant', [{ type: 'text', text: `${FENCE}\n\n${FENCE}` }])]);
    assert.equal(partsOf(out)[0].text.match(/An interactive chart was shown here/g)?.length, 2);
  });

  it('leaves user messages, other fences and non-text parts untouched', () => {
    const userMessage = makeMessage('user', [{ type: 'text', text: FENCE }]);
    const sqlPart = { type: 'text', text: '```sql\nSELECT 1\n```' };
    const toolPart = { type: 'tool-call', toolCallId: 'c1', input: FENCE };
    const assistantMessage = makeMessage('assistant', [sqlPart, toolPart]);
    const out = omitChartData([userMessage, assistantMessage]);
    assert.equal(out[0], userMessage);
    assert.equal(out[1], assistantMessage);
  });

  it('drops a lone fence filed after a _chart tool part, in either tool part shape', () => {
    const dynamicTool = { type: 'dynamic-tool', toolName: '_chart', toolCallId: 'c1', state: 'output-available' };
    const typedTool = { type: 'tool-_chart', toolCallId: 'c2', state: 'output-available' };
    for (const toolPart of [dynamicTool, typedTool]) {
      const [out] = omitChartData([makeMessage('assistant', [toolPart, { type: 'text', text: `\n${FENCE}\n` }])]);
      assert.deepEqual(partsOf(out), [toolPart]);
    }
  });

  it('drops every tool-filed chart after parallel _chart calls', () => {
    const tool = (id: string) => ({ type: 'dynamic-tool', toolName: '_chart', toolCallId: id });
    const prose = { type: 'text', text: 'Both charts are above.' };
    const parts = [tool('c1'), tool('c2'), { type: 'text', text: FENCE }, { type: 'text', text: FENCE }, prose];
    const [out] = omitChartData([makeMessage('assistant', parts)]);
    assert.deepEqual(partsOf(out), [tool('c1'), tool('c2'), prose]);
  });

  it('keeps a placeholder for fences the model wrote itself', () => {
    const chartTool = { type: 'dynamic-tool', toolName: '_chart', toolCallId: 'c1' };
    const otherTool = { type: 'dynamic-tool', toolName: 'sql__execute', toolCallId: 'c2' };
    const withProse = makeMessage('assistant', [chartTool, { type: 'text', text: `Trend:\n\n${FENCE}` }]);
    const beforeTool = makeMessage('assistant', [{ type: 'text', text: FENCE }, chartTool]);
    const otherToolOnly = makeMessage('assistant', [otherTool, { type: 'text', text: FENCE }]);
    for (const message of omitChartData([withProse, beforeTool, otherToolOnly])) {
      const texts = partsOf(message).filter((part) => part.type === 'text').map((part) => part.text);
      assert.equal(texts.length, 1);
      assert.ok(texts[0].includes('[An interactive chart was shown here (line'));
    }
  });

  it('drops a lone report fence filed after a _report tool part', () => {
    const report = '`````markdown-report:q3\n# Q3\n\n````\ncode\n````\n`````';
    for (const toolPart of [
      { type: 'dynamic-tool', toolName: '_report', toolCallId: 'r1' },
      { type: 'tool-_report', toolCallId: 'r2' },
    ]) {
      const [out] = omitChartData([makeMessage('assistant', [toolPart, { type: 'text', text: `\n${report}\n` }])]);
      assert.deepEqual(partsOf(out), [toolPart]);
    }
  });

  it('keeps report fences the model wrote itself', () => {
    const report = { type: 'text', text: '````markdown-report\n# Q3\n\nAll good.\n````' };
    const withoutTool = makeMessage('assistant', [report]);
    const afterChartTool = makeMessage('assistant', [{ type: 'dynamic-tool', toolName: '_chart', toolCallId: 'c1' }, report]);
    for (const message of [withoutTool, afterChartTool]) {
      const [out] = omitChartData([message]);
      assert.equal(out, message);
    }
  });

  it('does not mutate its input', () => {
    const part = { type: 'text', text: FENCE };
    const message = makeMessage('assistant', [part]);
    omitChartData([message]);
    assert.equal(part.text, FENCE);
    assert.equal(partsOf(message)[0], part);
  });
});
