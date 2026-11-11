import { strict as assert } from 'node:assert';
import { describe, it } from 'node:test';
import { TurnAborts } from '../../overrides/server/src/lib/turn-aborts.ts';

describe('TurnAborts', () => {
  it("stops the owner's running turn in the chat", () => {
    const turns = new TurnAborts();
    const signal = turns.start('s1', 'chat-1', 'alice');

    assert.equal(turns.stop({ chatId: 'chat-1', userId: 'alice' }), 1);
    assert.equal(signal.aborted, true);
  });

  it("ignores another user's turn and another chat", () => {
    const turns = new TurnAborts();
    const signal = turns.start('s1', 'chat-1', 'alice');

    assert.equal(turns.stop({ chatId: 'chat-1', userId: 'mallory' }), 0);
    assert.equal(turns.stop({ chatId: 'chat-2', userId: 'alice' }), 0);
    assert.equal(signal.aborted, false);
  });

  it('stops only the named turn when given a stream id', () => {
    const turns = new TurnAborts();
    const older = turns.start('s1', 'chat-1', 'alice');
    const newer = turns.start('s2', 'chat-1', 'alice');

    assert.equal(turns.stop({ chatId: 'chat-1', userId: 'alice', streamId: 's1' }), 1);
    assert.equal(older.aborted, true);
    assert.equal(newer.aborted, false);
  });

  it('forgets a finished turn', () => {
    const turns = new TurnAborts();
    const signal = turns.start('s1', 'chat-1', 'alice');
    turns.finish('s1');

    assert.equal(turns.stop({ chatId: 'chat-1', userId: 'alice' }), 0);
    assert.equal(signal.aborted, false);
  });

  it('stops a turn once', () => {
    const turns = new TurnAborts();
    turns.start('s1', 'chat-1', 'alice');
    turns.stop({ chatId: 'chat-1', userId: 'alice' });

    assert.equal(turns.stop({ chatId: 'chat-1', userId: 'alice' }), 0);
  });
});
