import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  MAX_IMAGE_EDGE,
  fitWithin,
} from '../../overrides/client/src/lib/image-downscale.ts';

describe('fitWithin', () => {
  it('leaves an image that already fits alone', () => {
    assert.equal(fitWithin(1024, 768, MAX_IMAGE_EDGE), undefined);
    assert.equal(fitWithin(MAX_IMAGE_EDGE, 10, MAX_IMAGE_EDGE), undefined);
  });

  it('scales the longest edge down and keeps the aspect ratio', () => {
    assert.deepEqual(fitWithin(4032, 3024, MAX_IMAGE_EDGE), {
      width: 1568,
      height: 1176,
    });
    assert.deepEqual(fitWithin(3024, 4032, MAX_IMAGE_EDGE), {
      width: 1176,
      height: 1568,
    });
  });

  it('never rounds a thin edge down to zero', () => {
    assert.deepEqual(fitWithin(10000, 1, MAX_IMAGE_EDGE), {
      width: 1568,
      height: 1,
    });
  });
});
