import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  type CodeNode,
  fenceLanguage,
  isInlineCode,
} from '../../overrides/client/src/components/elements/code-fence.ts';

// Streamdown gives inline code a single-line position and no language class;
// a fenced block spans multiple lines and (when tagged) carries `language-*`.
const singleLine: CodeNode = {
  position: { start: { line: 3 }, end: { line: 3 } },
};
const multiLine: CodeNode = {
  position: { start: { line: 3 }, end: { line: 6 } },
};

describe('fenceLanguage', () => {
  it('reads the language tag from a fenced block className', () => {
    assert.equal(fenceLanguage('language-python'), 'python');
  });

  it('stops at the first colon so download fences keep their base tag', () => {
    assert.equal(fenceLanguage('language-sql-download:q.sql:eyJ'), 'sql-download');
  });

  it('returns undefined for inline code (no language class)', () => {
    assert.equal(fenceLanguage(undefined), undefined);
    assert.equal(fenceLanguage('some-other-class'), undefined);
  });
});

describe('isInlineCode', () => {
  it('treats a tag-less single-line span as inline', () => {
    assert.equal(isInlineCode(singleLine, undefined), true);
  });

  it('treats a tag-less multi-line block as NOT inline', () => {
    // e.g. a language-less ``` fence — must render as a block, not collapse.
    assert.equal(isInlineCode(multiLine, undefined), false);
  });

  it('treats any language-tagged fence as NOT inline', () => {
    assert.equal(isInlineCode(singleLine, 'python'), false);
    assert.equal(isInlineCode(multiLine, 'python'), false);
  });

  it('falls back to inline for a tag-less span with no position metadata', () => {
    assert.equal(isInlineCode(undefined, undefined), true);
    assert.equal(isInlineCode({}, undefined), true);
  });
});
