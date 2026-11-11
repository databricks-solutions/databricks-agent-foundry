/**
 * Pure helpers for classifying a Streamdown `code` element as inline code vs
 * a fenced block, and for reading its language tag. Kept dependency-free (no
 * React / JSX) so it ships into the chat UI as a plain module AND can be unit
 * tested under `node --experimental-strip-types` (see
 * ../../../../../tests/ts/code-fence.test.ts). Used by `response.tsx`.
 */

/** Minimal shape of the unist node Streamdown passes to the `code` component. */
export type CodeNode = {
  position?: { start: { line: number }; end: { line: number } };
};

const LANGUAGE_TAG_RE = /language-([^:\s]+)/;

/**
 * The fenced-block language tag (e.g. `python`, `sql-download`) taken from a
 * code element's className, or `undefined` for inline code / an untagged
 * fence.
 */
export function fenceLanguage(
  className: string | undefined,
): string | undefined {
  return className?.match(LANGUAGE_TAG_RE)?.[1];
}

/**
 * Whether a Streamdown `code` element is inline code rather than a fenced
 * block. Mirrors upstream Streamdown: a tag-less span on a single source line
 * is inline. When position metadata is unavailable, a tag-less span is treated
 * as inline (matching the previous fallback behaviour).
 */
export function isInlineCode(
  node: CodeNode | undefined,
  lang: string | undefined,
): boolean {
  if (lang) {
    return false;
  }
  const pos = node?.position;
  if (!pos) {
    return true;
  }
  return pos.start.line === pos.end.line;
}
