/**
 * Pure helpers for the ``svg`` fence: the size limit, a cheap check before
 * parsing, and the ``data:`` URL the image is shown from. Kept
 * dependency-free so it can be unit tested under
 * `node --experimental-strip-types` (see
 * ../../../../../tests/ts/svg-fence.test.ts). Used by `svg-image.tsx`.
 *
 * The size limit mirrors `MAX_SVG_CHARS` in
 * payloads/agent/runtime/chat_rendering.py; keep the two in sync.
 */

export const SVG_FENCE_TAG = 'svg';
export const MAX_SVG_CHARS = 200_000;
export const SVG_CONTENT_TYPE = 'image/svg+xml;charset=utf-8';

// An optional XML declaration and comments, then the <svg> root. A
// DOCTYPE is not allowed: its entity declarations could expand to far
// more text than the fence holds.
const SVG_START_RE = /^\s*(?:<\?xml[^>]*\?>\s*)?(?:<!--[\s\S]*?-->\s*)*<svg[\s>]/;
const SVG_END_RE = /<\/svg>\s*$/;

/**
 * Whether *text* is worth parsing as SVG: within the size limit, starting
 * with an `<svg>` root and ending with `</svg>`. A fence that is still
 * streaming in fails the end check, so it is not parsed on every token.
 */
export function looksLikeSvg(text: string): boolean {
  return (
    text.length <= MAX_SVG_CHARS &&
    SVG_START_RE.test(text) &&
    SVG_END_RE.test(text)
  );
}

/** A `data:` URL that shows *svg* as an image. */
export function svgDataUrl(svg: string): string {
  return `data:${SVG_CONTENT_TYPE},${encodeURIComponent(svg)}`;
}
