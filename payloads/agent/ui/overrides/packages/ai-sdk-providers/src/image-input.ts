/**
 * Agent Bricks: carries chat images to Responses agents as `input_image`.
 *
 * `@databricks/ai-sdk-provider` (<= 0.5.x) throws on any non-text user
 * part in its Responses converter
 * (https://github.com/databricks/databricks-ai-bridge/issues/806).
 * `imageInputMiddleware` turns each image `file` part into a marker text
 * part the converter accepts, and `withInputImages` turns the resulting
 * `input_text` back into `input_image` in the outgoing request body.
 *
 * Delete the middleware and `withInputImages`, and their two call sites in
 * `providers-server.ts`, once the provider emits `input_image` itself.
 * `redactDataUrls` keeps image data out of the request log either way.
 */
import type {
  LanguageModelV3DataContent,
  LanguageModelV3Message,
  LanguageModelV3Middleware,
  LanguageModelV3Prompt,
} from '@ai-sdk/provider';

const IMAGE_MARKER = '[[agent-foundry:image]]';

// Only inline images of the composer's accepted types; anything else
// (remote URLs, other media types) is never rewritten.
const IMAGE_DATA_URL = /^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/]+=*$/;

type UserContentPart = Extract<
  LanguageModelV3Message,
  { role: 'user' }
>['content'][number];

interface ResponsesContentPart {
  type?: unknown;
  text?: unknown;
}

interface ResponsesInputItem {
  role?: unknown;
  content?: unknown;
}

function toImageDataUrl(
  data: LanguageModelV3DataContent,
  mediaType: string,
): string | undefined {
  let url: string;
  if (data instanceof URL) {
    url = data.href;
  } else if (typeof data === 'string') {
    url = data.startsWith('data:') ? data : `data:${mediaType};base64,${data}`;
  } else {
    url = `data:${mediaType};base64,${Buffer.from(data).toString('base64')}`;
  }
  return IMAGE_DATA_URL.test(url) ? url : undefined;
}

function markImagePart(part: UserContentPart): UserContentPart {
  if (part.type !== 'file' || !part.mediaType.startsWith('image/')) {
    return part;
  }
  const url = toImageDataUrl(part.data, part.mediaType);
  return url ? { type: 'text', text: `${IMAGE_MARKER}${url}` } : part;
}

/** *prompt* with every inlinable user image replaced by a marker text part. */
export function markImageParts(
  prompt: LanguageModelV3Prompt,
): LanguageModelV3Prompt {
  return prompt.map((message) =>
    message.role === 'user'
      ? { ...message, content: message.content.map(markImagePart) }
      : message,
  );
}

export const imageInputMiddleware: LanguageModelV3Middleware = {
  specificationVersion: 'v3',
  transformParams: async ({ params }) => ({
    ...params,
    prompt: markImageParts(params.prompt),
  }),
};

function toInputImage(part: ResponsesContentPart): unknown {
  if (part?.type !== 'input_text' || typeof part.text !== 'string') {
    return part;
  }
  if (!part.text.startsWith(IMAGE_MARKER)) {
    return part;
  }
  const url = part.text.slice(IMAGE_MARKER.length);
  return IMAGE_DATA_URL.test(url) ? { type: 'input_image', image_url: url } : part;
}

function withInputImagesInItem(item: ResponsesInputItem): ResponsesInputItem {
  if (item?.role !== 'user' || !Array.isArray(item.content)) {
    return item;
  }
  return { ...item, content: item.content.map(toInputImage) };
}

const BASE64_DATA_URL = /data:([\w.+-]+\/[\w.+-]+);base64,[A-Za-z0-9+/]+=*/g;

/** *text* with every base64 data URL replaced by its media type and length, for logging. */
export function redactDataUrls(text: string): string {
  return text.replace(
    BASE64_DATA_URL,
    (match, mediaType: string) => `data:${mediaType};base64,<${match.length} chars>`,
  );
}

/** *body* with image markers restored as `input_image`; unchanged when not JSON. */
export function withInputImages(body: string): string {
  if (!body.includes(IMAGE_MARKER)) {
    return body;
  }
  try {
    const request = JSON.parse(body);
    if (!Array.isArray(request.input)) {
      return body;
    }
    return JSON.stringify({
      ...request,
      input: request.input.map(withInputImagesInItem),
    });
  } catch {
    return body;
  }
}
