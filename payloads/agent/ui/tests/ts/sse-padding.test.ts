import { strict as assert } from 'node:assert';
import type { ServerResponse } from 'node:http';
import { describe, it } from 'node:test';
import { withSsePadding } from '../../overrides/server/src/lib/sse-padding.ts';

// Records the calls `pipeUIMessageStreamToResponse` makes, in order, so the
// suite needs no socket or HTTP server.
class RecordingResponse {
  readonly calls: Array<[string, unknown[]]> = [];

  writeHead(...args: unknown[]): this {
    this.calls.push(['writeHead', args]);
    return this;
  }

  write(chunk: unknown): boolean {
    this.calls.push(['write', [chunk]]);
    return true;
  }

  end(): this {
    this.calls.push(['end', []]);
    return this;
  }
}

function wrap(): { recorder: RecordingResponse; response: ServerResponse } {
  const recorder = new RecordingResponse();
  const response = withSsePadding(recorder as unknown as ServerResponse);
  return { recorder, response };
}

describe('withSsePadding', () => {
  it('writes one padding comment directly after the headers', () => {
    const { recorder, response } = wrap();

    response.writeHead(200, { 'content-type': 'text/event-stream' });
    response.write('data: {"type":"start"}\n\n');
    response.end();

    assert.deepEqual(
      recorder.calls.map(([name]) => name),
      ['writeHead', 'write', 'write', 'end'],
    );
    assert.deepEqual(recorder.calls[0][1], [
      200,
      { 'content-type': 'text/event-stream' },
    ]);
    assert.deepEqual(recorder.calls[2][1], ['data: {"type":"start"}\n\n']);
  });

  it('pads with an SSE comment that parsers ignore', () => {
    const { recorder, response } = wrap();

    response.writeHead(200);

    const [padding] = recorder.calls[1][1] as [string];
    assert.match(padding, /^: *\n\n$/);
    assert.ok(padding.length >= 2048);
  });

  it('passes writeHead with a status text through unchanged', () => {
    const { recorder, response } = wrap();

    response.writeHead(201, 'Created', { 'x-test': '1' });

    assert.deepEqual(recorder.calls[0][1], [201, 'Created', { 'x-test': '1' }]);
  });

  it('returns the underlying response from writeHead', () => {
    const { recorder, response } = wrap();

    assert.equal(response.writeHead(200), recorder);
  });
});
