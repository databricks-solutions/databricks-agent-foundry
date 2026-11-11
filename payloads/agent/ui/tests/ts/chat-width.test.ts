import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  chatWidthClass,
  DEFAULT_CHAT_WIDTH,
} from '../../overrides/client/src/lib/chat-width.ts';

describe('chatWidthClass', () => {
  it('maps every allowed width to its literal max-w class', () => {
    assert.equal(chatWidthClass('4xl'), 'max-w-4xl');
    assert.equal(chatWidthClass('5xl'), 'max-w-5xl');
    assert.equal(chatWidthClass('6xl'), 'max-w-6xl');
    assert.equal(chatWidthClass('7xl'), 'max-w-7xl');
  });

  it('reads a missing value as the default', () => {
    assert.equal(chatWidthClass(undefined), `max-w-${DEFAULT_CHAT_WIDTH}`);
    assert.equal(chatWidthClass(''), `max-w-${DEFAULT_CHAT_WIDTH}`);
  });

  it('reads an unknown value as the default', () => {
    assert.equal(chatWidthClass('8xl'), 'max-w-4xl');
    assert.equal(chatWidthClass('full'), 'max-w-4xl');
  });

  it('does not resolve inherited object keys', () => {
    assert.equal(chatWidthClass('constructor'), 'max-w-4xl');
    assert.equal(chatWidthClass('__proto__'), 'max-w-4xl');
  });

  it('keeps the default in step with the spec default', () => {
    assert.equal(DEFAULT_CHAT_WIDTH, '4xl');
  });
});
