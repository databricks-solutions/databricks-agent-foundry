// Stand-in for upstream's client/src/lib/ChatTransport.ts (the SSE
// transport): keeps the HttpChatTransport fields subclasses read and
// records every call that would have gone over SSE.
type Options = {
  api?: string;
  body?: object | (() => object | Promise<object>);
  prepareSendMessagesRequest?: (options: Record<string, unknown>) => { body: object };
};

export const sseCalls: string[] = [];
/** Set to make an SSE resume answer like a 204 (no running turn). */
export const sseResume = { empty: false };

export const sseStream = (): ReadableStream<{ type: string }> =>
  new ReadableStream({
    start(controller) {
      controller.enqueue({ type: 'sse' });
      controller.close();
    },
  });

export class ChatTransport {
  protected api: string;
  protected body: Options['body'];
  protected prepareSendMessagesRequest: Options['prepareSendMessagesRequest'];

  constructor(options: Options & Record<string, unknown> = {}) {
    this.api = options.api ?? '/api/chat';
    this.body = options.body;
    this.prepareSendMessagesRequest = options.prepareSendMessagesRequest;
  }

  async sendMessages(): Promise<ReadableStream<{ type: string }>> {
    sseCalls.push('send');
    return sseStream();
  }

  async reconnectToStream(): Promise<ReadableStream<{ type: string }> | null> {
    sseCalls.push('resume');
    return sseResume.empty ? null : sseStream();
  }
}
