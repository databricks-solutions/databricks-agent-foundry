/**
 * Chat column width cap, from `spec.ui.chat_layout.width`.
 *
 * The allowed keys mirror `ChatWidth` in `payloads/agent/contracts.py`.
 * Tailwind only generates classes it finds as literal strings in the
 * source, so each `max-w-*` class is spelled out here instead of being
 * built from the key (`max-w-${width}` would silently produce no CSS).
 */
const CHAT_WIDTH_CLASS = {
  '4xl': 'max-w-4xl',
  '5xl': 'max-w-5xl',
  '6xl': 'max-w-6xl',
  '7xl': 'max-w-7xl',
} as const;

export type ChatWidth = keyof typeof CHAT_WIDTH_CLASS;

/** Matches the spec default, so the column does not jump before `/api/config` resolves. */
export const DEFAULT_CHAT_WIDTH: ChatWidth = '4xl';

function isChatWidth(value: unknown): value is ChatWidth {
  return typeof value === 'string' && Object.hasOwn(CHAT_WIDTH_CLASS, value);
}

/** The `max-w-*` class for a configured width; unknown or missing values read as the default. */
export function chatWidthClass(width: unknown): string {
  return CHAT_WIDTH_CLASS[isChatWidth(width) ? width : DEFAULT_CHAT_WIDTH];
}
