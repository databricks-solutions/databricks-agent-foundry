/**
 * Agent Bricks: speech input helpers for the composer mic button
 * (docs/explanation/speech-to-text-design.md).
 *
 * Free of browser APIs so `payloads/agent/ui/tests/ts/` can test them;
 * `use-speech-recorder.ts` wires them to `MediaRecorder` and Web Audio.
 */

/** In preference order; the route accepts these base types. */
export const RECORDING_MIME_TYPES = [
  'audio/webm;codecs=opus',
  'audio/ogg;codecs=opus',
  'audio/mp4',
] as const;

/** About 1.2 MB for five minutes, well under the route's 2.5 MB body limit. */
export const AUDIO_BITS_PER_SECOND = 32_000;

/** RMS level (0..1) a recording must reach once to count as speech. */
export const SPEECH_LEVEL_THRESHOLD = 0.02;

const DEFAULT_ERROR = 'Could not transcribe this recording. Please try again.';
const ERROR_BY_STATUS: Readonly<Record<number, string>> = {
  413: 'The recording is too long.',
  429: 'A transcription is already running. Please wait for it to finish.',
  501: 'Speech input is not enabled for this app.',
  503: 'The speech service is busy. Please try again in a moment.',
  504: 'Transcription took too long. Please try a shorter recording.',
};
/** Failures where the same recording may succeed later; 0 is a network error. */
const RETRYABLE_STATUSES: ReadonlySet<number> = new Set([0, 502, 503, 504]);

export interface Insertion {
  text: string;
  cursor: number;
}

/** The first MIME type the browser can record, or `undefined` when it supports none. */
export function pickRecordingMimeType(
  isSupported: (mimeType: string) => boolean,
): string | undefined {
  return RECORDING_MIME_TYPES.find((mimeType) => isSupported(mimeType));
}

/** Root mean square of time-domain samples in -1..1. */
export function rmsLevel(samples: ArrayLike<number>): number {
  if (samples.length === 0) {
    return 0;
  }
  let sum = 0;
  for (let index = 0; index < samples.length; index += 1) {
    sum += samples[index] * samples[index];
  }
  return Math.sqrt(sum / samples.length);
}

/** Whether the loudest level seen while recording reached speech. */
export function heardSpeech(peakLevel: number, threshold = SPEECH_LEVEL_THRESHOLD): boolean {
  return peakLevel >= threshold;
}

/**
 * Inserts `transcript` at the end of the selection, separated by spaces where
 * it would touch other text. Typed text is never replaced.
 */
export function insertTranscript(
  current: string,
  transcript: string,
  selectionStart: number,
  selectionEnd: number,
): Insertion {
  const words = transcript.trim();
  const at = Math.min(Math.max(selectionStart, selectionEnd, 0), current.length);
  if (!words) {
    return { text: current, cursor: at };
  }
  const before = current.slice(0, at);
  const after = current.slice(at);
  const lead = before && !/\s$/.test(before) ? ' ' : '';
  const trail = after && !/^\s/.test(after) ? ' ' : '';
  const inserted = `${lead}${words}${trail}`;
  return { text: `${before}${inserted}${after}`, cursor: before.length + inserted.length };
}

/** The configured instance language, else the browser language, as a two-letter hint. */
export function languageHint(configured: string, browserLanguage: string | undefined): string | undefined {
  const candidate = configured.trim() || browserLanguage || '';
  const match = /^([a-z]{2})(?:[-_]|$)/i.exec(candidate);
  return match ? match[1].toLowerCase() : undefined;
}

/** The route's `{ error }` message, else a message for the HTTP status (proxy errors are plain text). */
export function transcriptionErrorMessage(status: number, body: unknown): string {
  const error = (body as { error?: unknown } | null)?.error;
  if (typeof error === 'string' && error.trim()) {
    return error;
  }
  return ERROR_BY_STATUS[status] ?? DEFAULT_ERROR;
}

/** Whether a failed transcription is worth offering a retry of the same recording. */
export function isRetryableStatus(status: number): boolean {
  return RETRYABLE_STATUSES.has(status);
}

/** `m:ss` for the recording timer. */
export function formatElapsed(totalSeconds: number): string {
  const seconds = Math.max(0, Math.floor(totalSeconds));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
