/**
 * Frames of the chat WebSocket (#772), shared by the browser transport and
 * the chat UI server.
 *
 * One socket carries one request: the client sends a single `send` or
 * `resume` frame, the server answers with `start`, then one `chunk` per UI
 * message chunk, then `end`, or with `error` at any point. A socket that
 * closes without `end` was dropped; the turn keeps running on the server and
 * can be resumed.
 */
import type { UIMessageChunk } from 'ai';
import type { ErrorCode } from './errors';

export const CHAT_SOCKET_PATH = '/api/chat/socket';

export type ChatSocketRequest =
  | { type: 'send'; body: unknown }
  | { type: 'resume'; chatId: string; cursor?: number };

export type ChatSocketFrame =
  | { type: 'start'; streamId: string }
  | { type: 'chunk'; index: number; chunk: UIMessageChunk }
  | { type: 'end' }
  | { type: 'error'; code: ErrorCode; cause?: string };

const ERROR_CODE = /^[a-z_]+:[a-z]+$/;

function parseObject(data: string): Record<string, unknown> | null {
  try {
    const value: unknown = JSON.parse(data);
    return typeof value === 'object' && value !== null && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function isIndex(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

export function encodeChatSocketMessage(
  message: ChatSocketRequest | ChatSocketFrame,
): string {
  return JSON.stringify(message);
}

/** The client's request frame, or `null` when it is malformed. */
export function decodeChatSocketRequest(data: string): ChatSocketRequest | null {
  const frame = parseObject(data);
  if (frame?.type === 'send' && typeof frame.body === 'object' && frame.body !== null) {
    return { type: 'send', body: frame.body };
  }
  if (frame?.type === 'resume' && typeof frame.chatId === 'string') {
    if (frame.cursor !== undefined && !isIndex(frame.cursor)) return null;
    return { type: 'resume', chatId: frame.chatId, cursor: frame.cursor };
  }
  return null;
}

/** A server frame, or `null` when it is malformed. */
export function decodeChatSocketFrame(data: string): ChatSocketFrame | null {
  const frame = parseObject(data);
  switch (frame?.type) {
    case 'start':
      return typeof frame.streamId === 'string'
        ? { type: 'start', streamId: frame.streamId }
        : null;
    case 'chunk':
      return isIndex(frame.index) && typeof frame.chunk === 'object' && frame.chunk !== null
        ? { type: 'chunk', index: frame.index, chunk: frame.chunk as UIMessageChunk }
        : null;
    case 'end':
      return { type: 'end' };
    case 'error':
      return typeof frame.code === 'string' && ERROR_CODE.test(frame.code)
        ? {
            type: 'error',
            code: frame.code as ErrorCode,
            cause: typeof frame.cause === 'string' ? frame.cause : undefined,
          }
        : null;
    default:
      return null;
  }
}
