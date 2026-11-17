import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  MAX_SVG_CHARS,
  looksLikeSvg,
  svgDataUrl,
} from '../../overrides/client/src/components/elements/svg-fence.ts';

const SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><circle r="4"/></svg>';

describe('looksLikeSvg', () => {
  it('accepts an svg root, with or without an XML declaration and comments', () => {
    assert.ok(looksLikeSvg(SVG));
    assert.ok(looksLikeSvg(`<?xml version="1.0"?>\n<!-- logo -->\n${SVG}\n`));
  });

  it('rejects an svg that is still streaming in', () => {
    assert.ok(!looksLikeSvg(SVG.slice(0, -10)));
  });

  it('rejects a DOCTYPE, whose entities could expand past the limit', () => {
    assert.ok(!looksLikeSvg(`<!DOCTYPE svg [<!ENTITY a "aaaa">]>${SVG}`));
  });

  it('rejects other documents and text', () => {
    assert.ok(!looksLikeSvg('<html><svg></svg></html>'));
    assert.ok(!looksLikeSvg('<svgfoo></svg>'));
    assert.ok(!looksLikeSvg('a circle'));
  });

  it('rejects an svg over the size limit', () => {
    const padding = ' '.repeat(MAX_SVG_CHARS);
    assert.ok(!looksLikeSvg(`<svg>${padding}</svg>`));
  });
});

describe('svgDataUrl', () => {
  it('encodes the svg into an image data URL', () => {
    const url = svgDataUrl('<svg a="#1"></svg>');
    assert.ok(url.startsWith('data:image/svg+xml;charset=utf-8,'));
    assert.equal(decodeURIComponent(url.split(',', 2)[1]), '<svg a="#1"></svg>');
  });
});
