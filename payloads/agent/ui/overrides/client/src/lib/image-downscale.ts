/**
 * Agent Bricks: shrink pasted / attached images before they travel inline.
 *
 * The image is re-sent as base64 with every later turn of the conversation,
 * so a phone photo at full resolution costs request size and model tokens
 * on each message. Vision models downsample beyond ~1568 px anyway.
 *
 * `fitWithin` is kept free of browser APIs so `payloads/agent/ui/tests/ts/`
 * can test it.
 */

export const MAX_IMAGE_EDGE = 1568;
export const IMAGE_QUALITY = 0.85;

export interface ImageSize {
  width: number;
  height: number;
}

/** Target size with the longest edge at `maxEdge`, or `undefined` if it already fits. */
export function fitWithin(
  width: number,
  height: number,
  maxEdge: number,
): ImageSize | undefined {
  const longest = Math.max(width, height);
  if (longest <= maxEdge) {
    return undefined;
  }
  const scale = maxEdge / longest;
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

function encode(canvas: HTMLCanvasElement, type: string): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob(
      (blob) =>
        blob ? resolve(blob) : reject(new Error('Image encoding failed')),
      type,
      IMAGE_QUALITY,
    );
  });
}

/**
 * The file itself when it already fits, otherwise a same-type re-encode with
 * the longest edge at `MAX_IMAGE_EDGE` -- whichever is smaller. The browser
 * ignores the quality argument for PNG.
 */
export async function downscaleImage(file: File): Promise<Blob> {
  const bitmap = await createImageBitmap(file);
  try {
    const target = fitWithin(bitmap.width, bitmap.height, MAX_IMAGE_EDGE);
    if (!target) {
      return file;
    }
    const canvas = document.createElement('canvas');
    canvas.width = target.width;
    canvas.height = target.height;
    const context = canvas.getContext('2d');
    if (!context) {
      return file;
    }
    context.drawImage(bitmap, 0, 0, target.width, target.height);
    const resized = await encode(canvas, file.type);
    return resized.size < file.size ? resized : file;
  } finally {
    bitmap.close();
  }
}
