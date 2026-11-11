import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  imageInputMiddleware,
  markImageParts,
  redactDataUrls,
  withInputImages,
} from '../../overrides/packages/ai-sdk-providers/src/image-input.ts';

const PNG_BASE64 = 'iVBORw0KGgo=';
const PNG_DATA_URL = `data:image/png;base64,${PNG_BASE64}`;

function userPrompt(...content: unknown[]) {
  return [{ role: 'user', content }] as never;
}

function onlyUserContent(prompt: unknown): unknown[] {
  return (prompt as Array<{ content: unknown[] }>)[0].content;
}

function requestBody(...content: unknown[]): string {
  return JSON.stringify({ input: [{ role: 'user', content }], stream: true });
}

describe('markImageParts', () => {
  it('turns a base64 image file part into a marker text part', () => {
    const [part] = onlyUserContent(
      markImageParts(userPrompt({ type: 'file', mediaType: 'image/png', data: PNG_BASE64 })),
    ) as Array<{ type: string; text: string }>;

    assert.equal(part.type, 'text');
    assert.ok(part.text.endsWith(PNG_DATA_URL));
  });

  it('accepts bytes and data URLs', () => {
    const bytes = Uint8Array.from(Buffer.from(PNG_BASE64, 'base64'));
    const content = onlyUserContent(
      markImageParts(
        userPrompt(
          { type: 'file', mediaType: 'image/png', data: bytes },
          { type: 'file', mediaType: 'image/png', data: new URL(PNG_DATA_URL) },
        ),
      ),
    ) as Array<{ type: string }>;

    assert.deepEqual(content.map((part) => part.type), ['text', 'text']);
  });

  it('leaves documents, remote URLs and unsupported image types untouched', () => {
    const parts = [
      { type: 'file', mediaType: 'application/pdf', data: 'JVBERi0=' },
      { type: 'file', mediaType: 'image/png', data: new URL('https://example.com/a.png') },
      { type: 'file', mediaType: 'image/svg+xml', data: 'PHN2Zz4=' },
      { type: 'text', text: 'hi' },
    ];

    assert.deepEqual(onlyUserContent(markImageParts(userPrompt(...parts))), parts);
  });

  it('only rewrites user messages', () => {
    const prompt = [{ role: 'system', content: 'be brief' }] as never;

    assert.deepEqual(markImageParts(prompt), prompt);
  });

  it('is applied by the middleware', async () => {
    const params = { prompt: userPrompt({ type: 'file', mediaType: 'image/png', data: PNG_BASE64 }) };
    const result = await imageInputMiddleware.transformParams!({
      type: 'stream',
      params: params as never,
      model: {} as never,
    });

    assert.equal((onlyUserContent(result.prompt)[0] as { type: string }).type, 'text');
  });
});

describe('withInputImages', () => {
  it('round-trips a marked image to input_image', () => {
    const [marked] = onlyUserContent(
      markImageParts(userPrompt({ type: 'file', mediaType: 'image/png', data: PNG_BASE64 })),
    ) as Array<{ text: string }>;
    const body = requestBody({ type: 'input_text', text: 'what is this?' }, { type: 'input_text', text: marked.text });

    assert.deepEqual(JSON.parse(withInputImages(body)), {
      input: [
        {
          role: 'user',
          content: [
            { type: 'input_text', text: 'what is this?' },
            { type: 'input_image', image_url: PNG_DATA_URL },
          ],
        },
      ],
      stream: true,
    });
  });

  it('does not rewrite a marker followed by a non-image URL', () => {
    const body = requestBody({ type: 'input_text', text: '[[agent-foundry:image]]https://example.com/a.png' });

    assert.equal(JSON.parse(withInputImages(body)).input[0].content[0].type, 'input_text');
  });

  it('does not rewrite markers outside user messages', () => {
    const body = JSON.stringify({
      input: [{ role: 'assistant', content: [{ type: 'input_text', text: `[[agent-foundry:image]]${PNG_DATA_URL}` }] }],
    });

    assert.equal(JSON.parse(withInputImages(body)).input[0].content[0].type, 'input_text');
  });

  it('returns bodies without a marker byte-for-byte', () => {
    const body = '{ "input": [] }';

    assert.equal(withInputImages(body), body);
  });

  it('leaves a non-JSON body unchanged', () => {
    assert.equal(withInputImages('[[agent-foundry:image]] not json'), '[[agent-foundry:image]] not json');
  });
});

describe('redactDataUrls', () => {
  it('replaces each data URL with its media type and length', () => {
    const body = requestBody(
      { type: 'input_image', image_url: PNG_DATA_URL },
      { type: 'input_text', text: `[[agent-foundry:image]]data:image/webp;base64,UklGRg==` },
    );

    const redacted = redactDataUrls(body);

    assert.ok(!redacted.includes(PNG_BASE64));
    assert.ok(redacted.includes(`data:image/png;base64,<${PNG_DATA_URL.length} chars>`));
    assert.ok(redacted.includes('data:image/webp;base64,<'));
  });

  it('leaves text without data URLs unchanged', () => {
    const body = requestBody({ type: 'input_text', text: 'see https://example.com/a.png' });

    assert.equal(redactDataUrls(body), body);
  });
});
