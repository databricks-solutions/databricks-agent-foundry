/**
 * Pure helpers for the ``markdown-report`` fence: its language tag and the
 * download filename. Kept dependency-free so it can be unit tested under
 * `node --experimental-strip-types` (see
 * ../../../../../tests/ts/report-fence.test.ts). Used by `response.tsx`.
 *
 * The filename rule mirrors `report_filename` in
 * payloads/agent/runtime/report_tool.py; keep the two in sync.
 */

export const REPORT_FENCE_TAG = 'markdown-report';

const MAX_FILENAME_CHARS = 80;
const DEFAULT_FILENAME = 'report';
const TAG_FILENAME_RE = new RegExp(`language-${REPORT_FENCE_TAG}:(\\S+)`);
const FIRST_HEADING_RE = /^#\s+(.+)$/m;

/** *text* as a file stem: lower-case letters and digits joined by single hyphens. */
export function reportFilenameStem(text: string): string {
  const stem = text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, MAX_FILENAME_CHARS)
    .replace(/-+$/, '');
  return stem || DEFAULT_FILENAME;
}

/**
 * The `.md` filename for a report: the name in the fence tag
 * (`markdown-report:<name>`), else the first `# ` heading, else `report`.
 */
export function reportFilename(className: string, markdown: string): string {
  const fromTag = className.match(TAG_FILENAME_RE)?.[1];
  const fromHeading = markdown.match(FIRST_HEADING_RE)?.[1];
  return `${reportFilenameStem(fromTag ?? fromHeading ?? '')}.md`;
}
