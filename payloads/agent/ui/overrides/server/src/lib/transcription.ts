/**
 * Speech-to-text contract and the `serving_invocations` backend
 * (docs/explanation/speech-to-text-design.md). Pure: no Express or upstream
 * imports, so it is unit-tested out of tree. Never logs audio or transcripts.
 */

export type TranscriptionErrorCode =
  | 'too_long'
  | 'unsupported_format'
  | 'empty_audio'
  | 'backend_unavailable'
  | 'overloaded'
  | 'timeout';

/** Codes only the route raises; backends never return them. */
export type RouteErrorCode = TranscriptionErrorCode | 'busy';

export type TranscriptionBackendName = 'fmapi_chat' | 'serving_invocations';

export interface TranscriptionRequest {
  audio: Uint8Array;
  mimeType: AudioMimeType;
  languageHint?: string;
}

export interface TranscriptionResult {
  text: string;
  detectedLanguage?: string;
  durationSeconds?: number;
}

export interface TranscriptionBackend {
  readonly name: TranscriptionBackendName;
  transcribe(request: TranscriptionRequest, signal: AbortSignal): Promise<TranscriptionResult>;
}

export interface WorkspaceCredentials {
  hostUrl: string;
  token: string;
}

export const AUDIO_MIME_TYPES = ['audio/webm', 'audio/ogg', 'audio/mp4'] as const;
export type AudioMimeType = (typeof AUDIO_MIME_TYPES)[number];

export const HTTP_STATUS_BY_CODE: Readonly<Record<RouteErrorCode, number>> = {
  busy: 429,
  too_long: 413,
  unsupported_format: 415,
  empty_audio: 422,
  backend_unavailable: 502,
  overloaded: 503,
  timeout: 504,
};

/** Model Serving answers these when the endpoint is at capacity or still scaling up. */
const OVERLOADED_STATUSES: ReadonlySet<number> = new Set([429, 503]);

const USER_MESSAGES: Readonly<Record<RouteErrorCode, string>> = {
  busy: 'A transcription is already running. Please wait for it to finish.',
  too_long: 'The recording is too long.',
  unsupported_format: 'This audio format is not supported.',
  empty_audio: 'No speech was detected in the recording.',
  backend_unavailable: 'Could not transcribe this recording. Please try again.',
  overloaded: 'The speech service is busy. Please try again in a moment.',
  timeout: 'Transcription took too long. Please try a shorter recording.',
};

export class TranscriptionError extends Error {
  readonly code: RouteErrorCode;

  constructor(code: RouteErrorCode) {
    super(USER_MESSAGES[code]);
    this.name = 'TranscriptionError';
    this.code = code;
  }
}

/** The allowed base MIME type of a Content-Type header, ignoring parameters such as `codecs`. */
export function audioMimeType(contentType: string | undefined): AudioMimeType | null {
  const base = (contentType ?? '').split(';')[0].trim().toLowerCase();
  return AUDIO_MIME_TYPES.find((type) => type === base) ?? null;
}

const LANGUAGE_TAG = /^([a-z]{2})(?:[-_][a-z0-9]{1,8})*$/i;

/** The two-letter ISO 639-1 code of a language tag (`de-DE` becomes `de`), or undefined. */
export function normalizeLanguageHint(raw: string | undefined): string | undefined {
  const match = LANGUAGE_TAG.exec((raw ?? '').trim());
  return match ? match[1].toLowerCase() : undefined;
}

/** Maps a parsed `/invocations` response of the Whisper wrapper to the contract. */
export function parseServingPrediction(payload: unknown): TranscriptionResult {
  const predictions = (payload as { predictions?: unknown } | null)?.predictions;
  const prediction = Array.isArray(predictions) ? predictions[0] : undefined;
  if (typeof prediction !== 'object' || prediction === null) {
    throw new TranscriptionError('backend_unavailable');
  }
  const { text, language, duration_seconds: duration } = prediction as Record<string, unknown>;
  if (typeof text !== 'string') {
    throw new TranscriptionError('backend_unavailable');
  }
  const trimmed = text.trim();
  if (!trimmed) {
    throw new TranscriptionError('empty_audio');
  }
  return {
    text: trimmed,
    detectedLanguage: typeof language === 'string' ? language : undefined,
    durationSeconds: typeof duration === 'number' ? duration : undefined,
  };
}

/** Calls a Model Serving endpoint that hosts the Whisper wrapper model. */
export class ServingInvocationsBackend implements TranscriptionBackend {
  readonly name = 'serving_invocations';
  private readonly endpoint: string;
  private readonly credentials: () => Promise<WorkspaceCredentials>;
  private readonly fetchFn: typeof fetch;

  constructor(endpoint: string, credentials: () => Promise<WorkspaceCredentials>, fetchFn: typeof fetch = fetch) {
    this.endpoint = endpoint;
    this.credentials = credentials;
    this.fetchFn = fetchFn;
  }

  async transcribe(request: TranscriptionRequest, signal: AbortSignal): Promise<TranscriptionResult> {
    const { hostUrl, token } = await this.credentials();
    const body = JSON.stringify({
      inputs: [
        {
          audio_base64: Buffer.from(request.audio).toString('base64'),
          mime_type: request.mimeType,
          language: request.languageHint ?? null,
        },
      ],
    });

    let response: Response;
    try {
      response = await this.fetchFn(`${hostUrl}/serving-endpoints/${encodeURIComponent(this.endpoint)}/invocations`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body,
        signal,
      });
    } catch {
      throw new TranscriptionError(signal.aborted ? 'timeout' : 'backend_unavailable');
    }

    if (!response.ok) {
      console.error(`[transcribe] serving endpoint answered ${response.status}`);
      throw new TranscriptionError(OVERLOADED_STATUSES.has(response.status) ? 'overloaded' : 'backend_unavailable');
    }
    const payload: unknown = await response.json().catch(() => null);
    if (signal.aborted) {
      throw new TranscriptionError('timeout');
    }
    return parseServingPrediction(payload);
  }
}
