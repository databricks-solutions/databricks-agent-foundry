import type { ServerResponse } from 'node:http';

/**
 * Safari (WebKit) can hold back the first small chunks of a streamed
 * `fetch()` body and hand the whole reply to the page only when the stream
 * ends. One SSE comment line sent right after the headers pushes the body
 * past that buffer, so later events reach the page as they are written.
 * SSE parsers ignore comment lines, so the client sees no extra event.
 */
const SSE_PADDING_BYTES = 2048;
const SSE_PADDING_COMMENT = `:${' '.repeat(SSE_PADDING_BYTES)}\n\n`;

function writeHeadWithPadding(
  this: ServerResponse,
  ...args: unknown[]
): ServerResponse {
  Reflect.apply(this.writeHead, this, args);
  this.write(SSE_PADDING_COMMENT);
  return this;
}

const paddingHandler: ProxyHandler<ServerResponse> = {
  get(target, property) {
    const value =
      property === 'writeHead'
        ? writeHeadWithPadding
        : Reflect.get(target, property, target);
    return typeof value === 'function' ? value.bind(target) : value;
  },
};

/**
 * Wraps a response so an SSE padding comment is written directly after
 * `writeHead`. Meant for `pipeUIMessageStreamToResponse`, which writes the
 * headers itself and would throw if anything was written before them.
 */
export function withSsePadding(response: ServerResponse): ServerResponse {
  return new Proxy(response, paddingHandler);
}
