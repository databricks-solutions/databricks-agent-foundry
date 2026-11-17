import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  reportFilename,
  reportFilenameStem,
} from '../../overrides/client/src/components/elements/report-fence.ts';

describe('reportFilenameStem', () => {
  it('keeps lower-case letters, digits and single hyphens', () => {
    assert.equal(reportFilenameStem('Q3 Cost Review'), 'q3-cost-review');
    assert.equal(reportFilenameStem('  Über / ../etc/passwd  '), 'ber-etc-passwd');
  });

  it('falls back to report when nothing usable is left', () => {
    assert.equal(reportFilenameStem('---'), 'report');
    assert.equal(reportFilenameStem(''), 'report');
  });

  it('is cut to 80 characters without a trailing hyphen', () => {
    const stem = reportFilenameStem(`${'a'.repeat(79)} b`);
    assert.ok(stem.length <= 80);
    assert.ok(!stem.endsWith('-'));
  });
});

describe('reportFilename', () => {
  it('prefers the name in the fence tag', () => {
    assert.equal(reportFilename('language-markdown-report:q3-review', '# Other'), 'q3-review.md');
  });

  it('sanitizes a name in the fence tag', () => {
    assert.equal(reportFilename('language-markdown-report:../../Evil.HTML', ''), 'evil-html.md');
  });

  it('uses the first heading when the tag has no name', () => {
    assert.equal(reportFilename('language-markdown-report', 'Intro\n\n# Weekly Status\n\n## Details'), 'weekly-status.md');
  });

  it('falls back to report.md', () => {
    assert.equal(reportFilename('language-markdown-report', 'No heading here.'), 'report.md');
  });
});
