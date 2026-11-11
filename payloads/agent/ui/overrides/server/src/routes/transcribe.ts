import express, {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { getDatabricksToken } from '@chat-template/auth';
import { getWorkspaceHostname } from '@chat-template/ai-sdk-providers';
import { authMiddleware, requireAuth } from '../middleware/auth';
import { envFlag, positiveIntFromEnv } from '../lib/env-flag';
import {
  AUDIO_MIME_TYPES,
  HTTP_STATUS_BY_CODE,
  ServingInvocationsBackend,
  TranscriptionError,
  audioMimeType,
  normalizeLanguageHint,
  type RouteErrorCode,
  type TranscriptionBackend,
  type TranscriptionRequest,
  type TranscriptionResult,
  type WorkspaceCredentials,
} from '../lib/transcription';

export const transcribeRouter: RouterType = Router();

// Per route, not router.use: the router is mounted at /api next to other routers.
const auth = [authMiddleware, requireAuth];

// Base64 grows the audio by a third; 2.5 MB stays under the 4 MB Foundation Model API cap.
const MAX_AUDIO_BYTES = '2.5mb';
// The Databricks Apps proxy cuts requests off at 120 s.
const ROUTE_TIMEOUT_MS = 100_000;
const DEFAULT_MAX_SECONDS = 300;
// Encoders pad the last frame, so a recording stopped at the limit can report slightly more.
const DURATION_GRACE_SECONDS = 2;

const rawAudio = express.raw({ type: [...AUDIO_MIME_TYPES], limit: MAX_AUDIO_BYTES });
const inFlightUsers = new Set<string>();

function sendError(res: Response, code: RouteErrorCode): Response {
  return res
    .status(HTTP_STATUS_BY_CODE[code])
    .json({ error: new TranscriptionError(code).message, code });
}

function bodyErrorCode(error: unknown): RouteErrorCode {
  return (error as { status?: number }).status === 413 ? 'too_long' : 'unsupported_format';
}

/** Reads the raw audio body with the route-local limit; resolves to an error code or null. */
function readAudioBody(req: Request, res: Response): Promise<RouteErrorCode | null> {
  return new Promise((resolve) => {
    rawAudio(req, res, (error?: unknown) => resolve(error ? bodyErrorCode(error) : null));
  });
}

async function workspaceCredentials(): Promise<WorkspaceCredentials> {
  const [token, hostUrl] = await Promise.all([getDatabricksToken(), getWorkspaceHostname()]);
  return { hostUrl, token };
}

function configuredBackend(): TranscriptionBackend | null {
  const endpoint = process.env.SPEECH_ENDPOINT?.trim();
  if (!endpoint || process.env.SPEECH_BACKEND?.trim() !== 'serving_invocations') {
    return null;
  }
  return new ServingInvocationsBackend(endpoint, workspaceCredentials);
}

/** Runs the backend until it answers, the route timeout passes or the client disconnects. */
async function transcribeWithin(
  backend: TranscriptionBackend,
  request: TranscriptionRequest,
  res: Response,
): Promise<TranscriptionResult> {
  const disconnect = new AbortController();
  const onClose = () => disconnect.abort();
  res.on('close', onClose);
  try {
    const signal = AbortSignal.any([AbortSignal.timeout(ROUTE_TIMEOUT_MS), disconnect.signal]);
    return await backend.transcribe(request, signal);
  } finally {
    res.off('close', onClose);
  }
}

/**
 * POST /api/transcribe
 *
 * Body: the raw recording (`audio/webm`, `audio/ogg` or `audio/mp4`).
 * Optional `X-Speech-Language` header with a language hint.
 * Answers `{ text, detectedLanguage?, durationSeconds? }`, or `{ error, code }`.
 * Auth to the backend is the app service principal; the transcript is never logged.
 */
transcribeRouter.post('/transcribe', ...auth, async (req: Request, res: Response) => {
  if (!envFlag('ENABLE_SPEECH_INPUT')) {
    return res.status(501).json({ error: 'Speech input is not enabled for this app.' });
  }
  const backend = configuredBackend();
  if (!backend) {
    return res.status(501).json({ error: 'Speech input is not configured for this app.' });
  }
  const mimeType = audioMimeType(req.headers['content-type']);
  if (!mimeType) {
    return sendError(res, 'unsupported_format');
  }
  const userId = req.session?.user.id;
  if (!userId) {
    return res.status(401).json({ error: 'Authentication required.' });
  }
  if (inFlightUsers.has(userId)) {
    return sendError(res, 'busy');
  }

  inFlightUsers.add(userId);
  try {
    const bodyError = await readAudioBody(req, res);
    if (bodyError) {
      return sendError(res, bodyError);
    }
    if (!Buffer.isBuffer(req.body) || req.body.length === 0) {
      return sendError(res, 'empty_audio');
    }
    const result = await transcribeWithin(
      backend,
      { audio: req.body, mimeType, languageHint: normalizeLanguageHint(req.get('x-speech-language')) },
      res,
    );
    const maxSeconds = positiveIntFromEnv('SPEECH_MAX_SECONDS', DEFAULT_MAX_SECONDS);
    if ((result.durationSeconds ?? 0) > maxSeconds + DURATION_GRACE_SECONDS) {
      return sendError(res, 'too_long');
    }
    return res.status(200).json(result);
  } catch (error) {
    if (error instanceof TranscriptionError) {
      return sendError(res, error.code);
    }
    console.error('[transcribe] unexpected error:', error instanceof Error ? error.name : typeof error);
    return sendError(res, 'backend_unavailable');
  } finally {
    inFlightUsers.delete(userId);
  }
});
