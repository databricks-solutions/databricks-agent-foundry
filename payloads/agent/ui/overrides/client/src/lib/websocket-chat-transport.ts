import type {
  HttpChatTransportInitOptions,
  UIMessage,
  UIMessageChunk,
} from 'ai';
import {
  CHAT_SOCKET_PATH,
  decodeChatSocketFrame,
  encodeChatSocketMessage,
  type ChatSocketRequest,
} from '@chat-template/core/chat-socket-protocol';
import { ChatSDKError } from '@chat-template/core/errors';
import { ChatTransport } from './ChatTransport';

/** Long enough for a cold proxy, short enough that a blocked socket barely shows. */
const OPEN_TIMEOUT_MS = 4_000;

/**
 * Set once a socket fails to open or drops mid-reply; the rest of the page
 * session then talks SSE so a blocking network costs one timeout, not one
 * per message.
 */
let socketUnavailable = false;

type SendOptions<T extends UIMessage> = Parameters<
  ChatTransport<T>['sendMessages']
>[0];
type ReconnectOptions<T extends UIMessage> = Parameters<
  ChatTransport<T>['reconnectToStream']
>[0];
type StreamPartHandler = (part: UIMessageChunk) => void;

export type WebSocketChatTransportOptions<T extends UIMessage> =
  HttpChatTransportInitOptions<T> & {
    onStreamPart: StreamPartHandler;
    /** Aborts the socket like `fetch`'s signal aborts the SSE request. */
    signal?: () => AbortSignal | undefined;
    /** The server's id for the turn a socket streams (SSE does not report it). */
    onTurnStart?: (streamId: string) => void;
    /** A resume found no running turn, over either transport. */
    onNoActiveTurn?: () => void;
  };

/** The socket never opened, or closed before the server confirmed the turn. */
class SocketUnavailableError extends Error {
  readonly opened: boolean;

  constructor(opened: boolean) {
    super('Chat socket unavailable');
    this.opened = opened;
  }
}

/**
 * Chat transport over the chat WebSocket (#772), falling back to the SSE
 * transport it extends when the socket is blocked. Cookies ride on the
 * same-origin upgrade; request headers from the AI SDK are not sent.
 */
export class WebSocketChatTransport<
  T extends UIMessage,
> extends ChatTransport<T> {
  private readonly onSocketPart: StreamPartHandler;
  private readonly signal: () => AbortSignal | undefined;
  private readonly onTurnStart: (streamId: string) => void;
  private readonly onNoActiveTurn: () => void;

  constructor({
    signal,
    onTurnStart,
    onNoActiveTurn,
    ...options
  }: WebSocketChatTransportOptions<T>) {
    super(options);
    this.onSocketPart = options.onStreamPart;
    this.signal = signal ?? (() => undefined);
    this.onTurnStart = onTurnStart ?? (() => undefined);
    this.onNoActiveTurn = onNoActiveTurn ?? (() => undefined);
  }

  override async sendMessages(
    options: SendOptions<T>,
  ): Promise<ReadableStream<UIMessageChunk>> {
    if (socketUnavailable) return super.sendMessages(options);
    const request: ChatSocketRequest = {
      type: 'send',
      body: await this.requestBody(options),
    };
    const signal = combineSignals(options.abortSignal, this.signal());
    try {
      return await this.openTurn(request, signal);
    } catch (error) {
      if (!(error instanceof SocketUnavailableError)) throw error;
      socketUnavailable = true;
      // Once open, the server may have started the turn; re-sending could
      // duplicate it, so surface the error and let onFinish resume.
      if (error.opened) throw new ChatSDKError('offline:chat');
      return super.sendMessages(options);
    }
  }

  override async reconnectToStream(
    options: ReconnectOptions<T>,
  ): Promise<ReadableStream<UIMessageChunk> | null> {
    const stream = await this.reconnect(options);
    if (stream === null) this.onNoActiveTurn();
    return stream;
  }

  private async reconnect(
    options: ReconnectOptions<T>,
  ): Promise<ReadableStream<UIMessageChunk> | null> {
    if (socketUnavailable) return super.reconnectToStream(options);
    const request: ChatSocketRequest = {
      type: 'resume',
      chatId: options.chatId,
    };
    try {
      return await this.openTurn(request, this.signal());
    } catch (error) {
      if (isEmptyStream(error)) return null;
      if (!(error instanceof SocketUnavailableError)) throw error;
      socketUnavailable = true;
      return super.reconnectToStream(options);
    }
  }

  private async openTurn(
    request: ChatSocketRequest,
    signal: AbortSignal | undefined,
  ): Promise<ReadableStream<UIMessageChunk>> {
    const turn = await openSocketStream(request, signal, this.onSocketPart);
    this.onTurnStart(turn.streamId);
    return turn.stream;
  }

  /** The POST body HttpChatTransport would send. */
  private async requestBody(options: SendOptions<T>): Promise<object> {
    const transportBody = await (typeof this.body === 'function'
      ? this.body()
      : this.body);
    const body = { ...transportBody, ...options.body };
    const prepared = await this.prepareSendMessagesRequest?.({
      api: this.api,
      id: options.chatId,
      messages: options.messages,
      body,
      headers: options.headers,
      credentials: undefined,
      requestMetadata: options.metadata,
      trigger: options.trigger,
      messageId: options.messageId,
    });
    return (
      prepared?.body ?? {
        ...body,
        id: options.chatId,
        messages: options.messages,
        trigger: options.trigger,
        messageId: options.messageId,
      }
    );
  }
}

/**
 * Resolves with the reply stream once the server sends `start`. A drop after
 * that closes the stream without `finish`, which chat.tsx resumes.
 */
function openSocketStream(
  request: ChatSocketRequest,
  signal: AbortSignal | undefined,
  onStreamPart: StreamPartHandler,
): Promise<SocketTurn> {
  return new Promise((resolve, reject) => {
    new SocketReader({ request, signal, onStreamPart, resolve, reject });
  });
}

interface SocketTurn {
  streamId: string;
  stream: ReadableStream<UIMessageChunk>;
}

interface SocketReaderOptions {
  request: ChatSocketRequest;
  signal: AbortSignal | undefined;
  onStreamPart: StreamPartHandler;
  resolve: (turn: SocketTurn) => void;
  reject: (error: unknown) => void;
}

class SocketReader {
  private readonly options: SocketReaderOptions;
  private readonly ws = new WebSocket(socketUrl());
  private readonly openTimer: ReturnType<typeof setTimeout>;
  private readonly onAbort = () =>
    this.fail(new DOMException('The chat request was aborted.', 'AbortError'));
  private controller: ReadableStreamDefaultController<UIMessageChunk> | null =
    null;
  private opened = false;
  private finished = false;

  constructor(options: SocketReaderOptions) {
    this.options = options;
    this.openTimer = setTimeout(() => this.ws.close(), OPEN_TIMEOUT_MS);
    this.ws.onopen = () => this.onOpen();
    this.ws.onmessage = (event: MessageEvent) => this.onMessage(event);
    this.ws.onclose = () => this.onClose();
    if (options.signal?.aborted) this.onAbort();
    else options.signal?.addEventListener('abort', this.onAbort);
  }

  private onOpen(): void {
    clearTimeout(this.openTimer);
    this.opened = true;
    this.ws.send(encodeChatSocketMessage(this.options.request));
  }

  private onMessage(event: MessageEvent): void {
    const frame =
      typeof event.data === 'string' ? decodeChatSocketFrame(event.data) : null;
    if (frame?.type === 'start' && !this.controller) {
      this.options.resolve({
        streamId: frame.streamId,
        stream: new ReadableStream<UIMessageChunk>({
          start: (controller) => {
            this.controller = controller;
          },
          cancel: () => this.ws.close(),
        }),
      });
    } else if (frame?.type === 'chunk' && this.controller) {
      this.options.onStreamPart(frame.chunk);
      this.controller.enqueue(frame.chunk);
    } else if (frame?.type === 'end' && this.controller) {
      this.settle();
      this.controller.close();
    } else if (frame?.type === 'error') {
      this.fail(new ChatSDKError(frame.code, frame.cause));
    } else {
      this.fail(new ChatSDKError('bad_request:stream'));
    }
  }

  private onClose(): void {
    if (this.finished) return;
    this.settle();
    if (this.controller) {
      socketUnavailable = true;
      this.controller.close();
    } else {
      this.options.reject(new SocketUnavailableError(this.opened));
    }
  }

  private fail(error: unknown): void {
    if (this.finished) return;
    this.settle();
    if (this.controller) this.controller.error(error);
    else this.options.reject(error);
  }

  private settle(): void {
    this.finished = true;
    clearTimeout(this.openTimer);
    this.options.signal?.removeEventListener('abort', this.onAbort);
    this.ws.close();
  }
}

function socketUrl(): string {
  const url = new URL(CHAT_SOCKET_PATH, window.location.href);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  return url.href;
}

function combineSignals(
  ...signals: (AbortSignal | undefined)[]
): AbortSignal | undefined {
  const present = signals.filter((s): s is AbortSignal => s !== undefined);
  return present.length > 1 ? AbortSignal.any(present) : present[0];
}

function isEmptyStream(error: unknown): boolean {
  return (
    error instanceof ChatSDKError &&
    error.type === 'empty' &&
    error.surface === 'stream'
  );
}
