import {
  Router,
  type Request,
  type Response,
  type Router as RouterType,
} from 'express';
import { authMiddleware, requireAuth } from '../middleware/auth';
import { envFlag } from '../lib/env-flag';

export const filesRouter: RouterType = Router();

// Populate req.session before requireAuth runs (matches every other
// router, e.g. historyRouter). Without this, requireAuth always sees an
// undefined session and rejects every upload with 401.
filesRouter.use(authMiddleware);

function extractorUrl(): string {
  const port = process.env.FILE_UPLOAD_PORT || '8001';
  return `http://localhost:${port}/extract`;
}

/**
 * POST /api/files/upload
 *
 * Thin reverse-proxy to the isolated extractor service (:8001). The raw
 * multipart body is streamed straight through, so the Node server needs no
 * multipart parser of its own (the FastAPI extractor parses it) and no extra
 * dependency in `server/package.json`. The OBO user token is forwarded for
 * parity with the chat route.
 *
 * Response shape (consumed by the multimodal-input override):
 *   { name, format, extractedText, truncated }
 * Errors surface the extractor's user-safe `detail` as `{ error }`.
 */
filesRouter.post('/upload', requireAuth, async (req: Request, res: Response) => {
  if (!envFlag('ENABLE_FILE_UPLOAD')) {
    return res
      .status(501)
      .json({ error: 'File upload is not enabled for this app.' });
  }

  const contentType = req.headers['content-type'];
  if (!contentType || !contentType.includes('multipart/form-data')) {
    return res
      .status(400)
      .json({ error: 'Expected a multipart/form-data upload.' });
  }

  const headers: Record<string, string> = { 'content-type': contentType };
  if (req.headers['content-length']) {
    headers['content-length'] = req.headers['content-length'] as string;
  }
  const oboToken = req.headers['x-forwarded-access-token'];
  if (oboToken) {
    headers['x-forwarded-access-token'] = oboToken as string;
  }

  try {
    const upstream = await fetch(extractorUrl(), {
      method: 'POST',
      headers,
      // Stream the inbound request body straight to the extractor; Node
      // streaming request bodies require half-duplex mode.
      body: req as unknown as ReadableStream,
      duplex: 'half',
    } as RequestInit & { duplex: 'half' });

    const payload = (await upstream.json().catch(() => ({}))) as {
      detail?: string;
      filename?: string;
      format?: string;
      text?: string;
      truncated?: boolean;
    };

    if (!upstream.ok) {
      return res
        .status(upstream.status)
        .json({ error: payload.detail ?? 'File extraction failed.' });
    }

    return res.status(200).json({
      name: payload.filename,
      format: payload.format,
      extractedText: payload.text,
      truncated: payload.truncated ?? false,
    });
  } catch (error) {
    console.error('[files] extractor proxy error:', error);
    return res
      .status(502)
      .json({ error: 'Could not reach the extraction service.' });
  }
});
