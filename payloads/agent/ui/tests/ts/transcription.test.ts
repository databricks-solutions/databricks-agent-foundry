import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  HTTP_STATUS_BY_CODE,
  ServingInvocationsBackend,
  TranscriptionError,
  audioMimeType,
  normalizeLanguageHint,
  parseServingPrediction,
  type TranscriptionRequest,
} from '../../overrides/server/src/lib/transcription.ts';

const CREDENTIALS = async () => ({ hostUrl: 'https://example.cloud.databricks.com', token: 'tok' });
const REQUEST: TranscriptionRequest = { audio: new Uint8Array([1, 2, 3]), mimeType: 'audio/webm', languageHint: 'de' };

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

async function rejectsWith(promise: Promise<unknown>, code: string): Promise<void> {
  await assert.rejects(promise, (error: unknown) => error instanceof TranscriptionError && error.code === code);
}

describe('audioMimeType', () => {
  it('accepts the allowed types and ignores parameters and case', () => {
    assert.equal(audioMimeType('audio/webm;codecs=opus'), 'audio/webm');
    assert.equal(audioMimeType('Audio/OGG'), 'audio/ogg');
    assert.equal(audioMimeType('audio/mp4'), 'audio/mp4');
  });

  for (const contentType of [undefined, '', 'audio/flac', 'application/json', 'audio/webmx']) {
    it(`rejects ${JSON.stringify(contentType)}`, () => {
      assert.equal(audioMimeType(contentType), null);
    });
  }
});

describe('normalizeLanguageHint', () => {
  it('reduces a language tag to its two-letter code', () => {
    assert.equal(normalizeLanguageHint('de-DE'), 'de');
    assert.equal(normalizeLanguageHint(' EN_us '), 'en');
    assert.equal(normalizeLanguageHint('fr'), 'fr');
  });

  for (const raw of [undefined, '', 'deu', 'd', 'de-', '*', 'de;q=0.9']) {
    it(`drops ${JSON.stringify(raw)}`, () => {
      assert.equal(normalizeLanguageHint(raw), undefined);
    });
  }
});

describe('parseServingPrediction', () => {
  it('maps the wrapper output to the contract', () => {
    assert.deepEqual(
      parseServingPrediction({ predictions: [{ text: ' Hallo Welt ', language: 'de', duration_seconds: 2.5 }] }),
      { text: 'Hallo Welt', detectedLanguage: 'de', durationSeconds: 2.5 },
    );
  });

  it('treats empty text as empty audio', () => {
    assert.throws(
      () => parseServingPrediction({ predictions: [{ text: '  ', language: 'en', duration_seconds: 10 }] }),
      (error: unknown) => error instanceof TranscriptionError && error.code === 'empty_audio',
    );
  });

  for (const payload of [null, {}, { predictions: [] }, { predictions: [{ language: 'en' }] }, { predictions: 'x' }]) {
    it(`rejects ${JSON.stringify(payload)} as backend_unavailable`, () => {
      assert.throws(
        () => parseServingPrediction(payload),
        (error: unknown) => error instanceof TranscriptionError && error.code === 'backend_unavailable',
      );
    });
  }
});

describe('ServingInvocationsBackend', () => {
  it('posts the base64 audio, MIME type and hint to the endpoint', async () => {
    let captured: { url: string; init: RequestInit } | undefined;
    const fetchFn = (async (url: string, init: RequestInit) => {
      captured = { url, init };
      return jsonResponse(200, { predictions: [{ text: 'ok', language: 'de', duration_seconds: 1 }] });
    }) as unknown as typeof fetch;
    const backend = new ServingInvocationsBackend('speech whisper', CREDENTIALS, fetchFn);

    const result = await backend.transcribe(REQUEST, new AbortController().signal);

    assert.deepEqual(result, { text: 'ok', detectedLanguage: 'de', durationSeconds: 1 });
    assert.equal(captured?.url, 'https://example.cloud.databricks.com/serving-endpoints/speech%20whisper/invocations');
    assert.equal((captured?.init.headers as Record<string, string>).Authorization, 'Bearer tok');
    assert.deepEqual(JSON.parse(captured?.init.body as string), {
      inputs: [{ audio_base64: 'AQID', mime_type: 'audio/webm', language: 'de' }],
    });
  });

  it('sends a null language without a hint', async () => {
    let body = '';
    const fetchFn = (async (_url: string, init: RequestInit) => {
      body = init.body as string;
      return jsonResponse(200, { predictions: [{ text: 'ok' }] });
    }) as unknown as typeof fetch;
    const backend = new ServingInvocationsBackend('ep', CREDENTIALS, fetchFn);

    await backend.transcribe({ ...REQUEST, languageHint: undefined }, new AbortController().signal);

    assert.equal(JSON.parse(body).inputs[0].language, null);
  });

  for (const status of [400, 404, 500]) {
    it(`maps ${status} to backend_unavailable`, async () => {
      const fetchFn = (async () => jsonResponse(status, { error_code: 'X' })) as unknown as typeof fetch;
      const backend = new ServingInvocationsBackend('ep', CREDENTIALS, fetchFn);

      await rejectsWith(backend.transcribe(REQUEST, new AbortController().signal), 'backend_unavailable');
    });
  }

  for (const status of [429, 503]) {
    it(`maps ${status} to overloaded`, async () => {
      const fetchFn = (async () => jsonResponse(status, { error_code: 'X' })) as unknown as typeof fetch;
      const backend = new ServingInvocationsBackend('ep', CREDENTIALS, fetchFn);

      await rejectsWith(backend.transcribe(REQUEST, new AbortController().signal), 'overloaded');
    });
  }

  it('maps a network failure to backend_unavailable', async () => {
    const fetchFn = (async () => {
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;
    const backend = new ServingInvocationsBackend('ep', CREDENTIALS, fetchFn);

    await rejectsWith(backend.transcribe(REQUEST, new AbortController().signal), 'backend_unavailable');
  });

  it('maps an aborted call to timeout', async () => {
    const controller = new AbortController();
    const fetchFn = (async () => {
      controller.abort();
      throw new DOMException('aborted', 'AbortError');
    }) as unknown as typeof fetch;
    const backend = new ServingInvocationsBackend('ep', CREDENTIALS, fetchFn);

    await rejectsWith(backend.transcribe(REQUEST, controller.signal), 'timeout');
  });
});

describe('HTTP_STATUS_BY_CODE', () => {
  it('maps every route error code to the documented status', () => {
    assert.deepEqual(HTTP_STATUS_BY_CODE, {
      busy: 429,
      too_long: 413,
      unsupported_format: 415,
      empty_audio: 422,
      backend_unavailable: 502,
      overloaded: 503,
      timeout: 504,
    });
  });

  it('gives every code a user-safe message', () => {
    for (const code of Object.keys(HTTP_STATUS_BY_CODE) as (keyof typeof HTTP_STATUS_BY_CODE)[]) {
      assert.ok(new TranscriptionError(code).message.length > 0);
    }
  });
});
