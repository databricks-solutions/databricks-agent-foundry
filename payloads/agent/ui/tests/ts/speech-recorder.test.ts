import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import {
  RECORDING_MIME_TYPES,
  SPEECH_LEVEL_THRESHOLD,
  formatElapsed,
  heardSpeech,
  insertTranscript,
  isRetryableStatus,
  languageHint,
  pickRecordingMimeType,
  rmsLevel,
  transcriptionErrorMessage,
} from '../../overrides/client/src/lib/speech-recorder.ts';

describe('pickRecordingMimeType', () => {
  it('prefers webm/opus', () => {
    assert.equal(pickRecordingMimeType(() => true), 'audio/webm;codecs=opus');
  });

  it('falls back to ogg for Firefox and mp4 for Safari', () => {
    assert.equal(pickRecordingMimeType((type) => type.startsWith('audio/ogg')), 'audio/ogg;codecs=opus');
    assert.equal(pickRecordingMimeType((type) => type === 'audio/mp4'), 'audio/mp4');
  });

  it('is undefined when nothing is supported', () => {
    assert.equal(pickRecordingMimeType(() => false), undefined);
  });

  it('only offers types the transcribe route accepts', () => {
    const routeTypes = new Set(['audio/webm', 'audio/ogg', 'audio/mp4']);
    for (const type of RECORDING_MIME_TYPES) {
      assert.ok(routeTypes.has(type.split(';')[0]), type);
    }
  });
});

describe('silence check', () => {
  it('measures the RMS level', () => {
    assert.equal(rmsLevel([]), 0);
    assert.equal(rmsLevel([0, 0, 0]), 0);
    assert.equal(rmsLevel([0.5, -0.5]), 0.5);
  });

  it('counts speech only from the threshold up', () => {
    assert.equal(heardSpeech(0), false);
    assert.equal(heardSpeech(SPEECH_LEVEL_THRESHOLD / 2), false);
    assert.equal(heardSpeech(SPEECH_LEVEL_THRESHOLD), true);
    assert.equal(heardSpeech(Number.POSITIVE_INFINITY), true);
  });
});

describe('insertTranscript', () => {
  it('fills an empty composer', () => {
    assert.deepEqual(insertTranscript('', ' Hallo Welt ', 0, 0), { text: 'Hallo Welt', cursor: 10 });
  });

  it('separates the transcript from typed text with spaces', () => {
    assert.deepEqual(insertTranscript('Show me sales', 'for March', 13, 13), {
      text: 'Show me sales for March',
      cursor: 23,
    });
    assert.equal(insertTranscript('Show sales', 'all', 4, 4).text, 'Show all sales');
  });

  it('keeps existing whitespace instead of doubling it', () => {
    assert.equal(insertTranscript('Show ', 'sales', 5, 5).text, 'Show sales');
    assert.equal(insertTranscript(' now', 'Do it', 0, 0).text, 'Do it now');
  });

  it('never replaces a selection: it inserts after it', () => {
    assert.equal(insertTranscript('keep this', 'and that', 0, 9).text, 'keep this and that');
  });

  it('clamps a cursor past the end and ignores an empty transcript', () => {
    assert.equal(insertTranscript('text', 'more', 99, 99).text, 'text more');
    assert.deepEqual(insertTranscript('text', '  ', 2, 2), { text: 'text', cursor: 2 });
  });
});

describe('languageHint', () => {
  it('prefers the instance language over the browser language', () => {
    assert.equal(languageHint('de', 'en-US'), 'de');
  });

  it('falls back to the browser primary subtag', () => {
    assert.equal(languageHint('', 'en-US'), 'en');
    assert.equal(languageHint(' ', 'FR'), 'fr');
  });

  it('is undefined without a usable language', () => {
    assert.equal(languageHint('', undefined), undefined);
    assert.equal(languageHint('', 'x-klingon'), undefined);
  });
});

describe('transcriptionErrorMessage', () => {
  it('uses the route message', () => {
    assert.equal(
      transcriptionErrorMessage(422, { error: 'No speech was detected in the recording.', code: 'empty_audio' }),
      'No speech was detected in the recording.',
    );
  });

  it('maps statuses without a JSON body, such as proxy errors', () => {
    assert.equal(transcriptionErrorMessage(504, null), 'Transcription took too long. Please try a shorter recording.');
    assert.equal(transcriptionErrorMessage(503, 'upstream connect error'), 'The speech service is busy. Please try again in a moment.');
    assert.equal(transcriptionErrorMessage(500, 'upstream connect error'), 'Could not transcribe this recording. Please try again.');
  });
});

describe('isRetryableStatus', () => {
  for (const status of [0, 502, 503, 504]) {
    it(`offers a retry after ${status}`, () => {
      assert.equal(isRetryableStatus(status), true);
    });
  }

  for (const status of [413, 415, 422, 429, 501]) {
    it(`offers no retry after ${status}`, () => {
      assert.equal(isRetryableStatus(status), false);
    });
  }
});

describe('formatElapsed', () => {
  it('renders m:ss', () => {
    assert.equal(formatElapsed(0), '0:00');
    assert.equal(formatElapsed(65.9), '1:05');
    assert.equal(formatElapsed(300), '5:00');
  });
});
