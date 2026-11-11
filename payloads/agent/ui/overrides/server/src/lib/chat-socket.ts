/**
 * WebSocket transport for the chat turn (#772): one socket per request at
 * CHAT_SOCKET_PATH, framed by chat-socket-protocol. The SSE routes in
 * routes/chat.ts stay as the client's fallback.
 *
 * Closing the socket only stops this reader; the turn keeps draining into
 * the stream cache and stays resumable.
 */
import { STATUS_CODES, type IncomingMessage, type Server } from 'node:http';
import type { Duplex } from 'node:stream';
import { WebSocket, WebSocketServer } from 'ws';
import { getAuthSession } from '@chat-template/auth';
import {
  CHAT_SOCKET_PATH,
  decodeChatSocketRequest,
  encodeChatSocketMessage,
  type ChatSocketFrame,
  type ChatSocketRequest,
} from '@chat-template/core/chat-socket-protocol';
import { ChatSDKError } from '@chat-template/core/errors';
import {
  asChatSDKError,
  resumeChatTurn,
  startChatTurn,
  type ChatTurn,
  type ChatTurnContext,
} from './chat-turn';

/** Same limit as the express.json body parser. */
const MAX_PAYLOAD_BYTES = 10 * 1024 * 1024;
const REQUEST_TIMEOUT_MS = 10_000;
/** Below the idle timeouts of common reverse proxies. */
const PING_INTERVAL_MS = 25_000;
const NORMAL_CLOSURE = 1000;

export function attachChatSocket(server: Server): void {
  const wss = new WebSocketServer({
    noServer: true,
    maxPayload: MAX_PAYLOAD_BYTES,
  });
  server.on('upgrade', (req: IncomingMessage, socket: Duplex, head: Buffer) => {
    void handleUpgrade(wss, req, socket, head);
  });
}

async function handleUpgrade(
  wss: WebSocketServer,
  req: IncomingMessage,
  socket: Duplex,
  head: Buffer,
): Promise<void> {
  if (requestPath(req) !== CHAT_SOCKET_PATH) {
    socket.destroy();
    return;
  }
  if (!isSameOrigin(req)) {
    rejectUpgrade(socket, 403);
    return;
  }
  try {
    const session = await getAuthSession({
      getRequestHeader: (name: string) => firstHeader(req, name),
    });
    if (!session?.user) {
      rejectUpgrade(socket, 401);
      return;
    }
    const ctx: ChatTurnContext = { session, headers: req.headers };
    wss.handleUpgrade(req, socket, head, (ws: WebSocket) => {
      void serveChatSocket(ws, ctx);
    });
  } catch (error) {
    console.error('[chat-socket] Upgrade failed:', error);
    rejectUpgrade(socket, 500);
  }
}

async function serveChatSocket(
  ws: WebSocket,
  ctx: ChatTurnContext,
): Promise<void> {
  const heartbeat = setInterval(() => ws.ping(), PING_INTERVAL_MS);
  ws.once('close', () => clearInterval(heartbeat));
  try {
    const request = await readRequest(ws);
    const turn = await openTurn(ctx, request);
    const firstIndex = request.type === 'resume' ? (request.cursor ?? 0) : 0;
    await relayTurn(ws, turn, firstIndex);
    ws.close(NORMAL_CLOSURE);
  } catch (error) {
    closeWithError(ws, error);
  }
}

function readRequest(ws: WebSocket): Promise<ChatSocketRequest> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new ChatSDKError('bad_request:api', 'request_timeout')),
      REQUEST_TIMEOUT_MS,
    );
    ws.once('message', (data: WebSocket.RawData, isBinary: boolean) => {
      clearTimeout(timer);
      const request = isBinary ? null : decodeChatSocketRequest(data.toString());
      if (request) resolve(request);
      else reject(new ChatSDKError('bad_request:api'));
    });
    ws.once('close', () => {
      clearTimeout(timer);
      reject(new ChatSDKError('bad_request:api', 'closed'));
    });
  });
}

function openTurn(
  ctx: ChatTurnContext,
  request: ChatSocketRequest,
): Promise<ChatTurn> {
  return request.type === 'send'
    ? startChatTurn(ctx, request.body)
    : resumeChatTurn(ctx, request.chatId, request.cursor);
}

async function relayTurn(
  ws: WebSocket,
  turn: ChatTurn,
  firstIndex: number,
): Promise<void> {
  const reader = turn.stream.getReader();
  ws.once('close', () => void reader.cancel());
  await sendFrame(ws, { type: 'start', streamId: turn.streamId });
  for (let index = firstIndex; ; index++) {
    const { done, value } = await reader.read();
    if (done) break;
    await sendFrame(ws, { type: 'chunk', index, chunk: value });
  }
  await sendFrame(ws, { type: 'end' });
}

/** Resolves once the frame is flushed, so a slow client throttles the relay. */
function sendFrame(ws: WebSocket, frame: ChatSocketFrame): Promise<void> {
  return new Promise((resolve, reject) => {
    ws.send(encodeChatSocketMessage(frame), (error?: Error) =>
      error ? reject(error) : resolve(),
    );
  });
}

/** A closed socket means the client left; the turn itself is unaffected. */
function closeWithError(ws: WebSocket, error: unknown): void {
  if (ws.readyState !== WebSocket.OPEN) return;
  if (!(error instanceof ChatSDKError)) {
    console.error('[chat-socket] Turn failed:', error);
  }
  const chatError = asChatSDKError(error);
  const { json } = chatError.toResponse();
  ws.send(
    encodeChatSocketMessage({
      type: 'error',
      code: `${chatError.type}:${chatError.surface}`,
      cause: 'cause' in json ? json.cause : undefined,
    }),
  );
  ws.close(NORMAL_CLOSURE);
}

function requestPath(req: IncomingMessage): string {
  return new URL(req.url ?? '/', 'http://localhost').pathname;
}

function firstHeader(req: IncomingMessage, name: string): string | null {
  const value = req.headers[name.toLowerCase()];
  return (Array.isArray(value) ? value[0] : value) ?? null;
}

/** Blocks cross-site WebSocket hijacking; browsers always send Origin. */
function isSameOrigin(req: IncomingMessage): boolean {
  const origin = firstHeader(req, 'origin');
  const host =
    firstHeader(req, 'x-forwarded-host')?.split(',')[0]?.trim() ??
    firstHeader(req, 'host');
  if (!origin || !host) return false;
  try {
    return new URL(origin).host === host;
  } catch {
    return false;
  }
}

function rejectUpgrade(socket: Duplex, status: number): void {
  socket.end(
    `HTTP/1.1 ${status} ${STATUS_CODES[status]}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`,
  );
}
