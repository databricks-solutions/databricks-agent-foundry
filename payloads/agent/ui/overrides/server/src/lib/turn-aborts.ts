/**
 * Running chat turns their owner can stop (#775). A turn registers when it
 * starts and leaves when it finishes; closing a connection does not stop it.
 */
interface RunningTurn {
  chatId: string;
  userId: string;
  controller: AbortController;
}

export interface StopRequest {
  chatId: string;
  userId: string;
  /** Only this turn; without it, every running turn of the user in the chat. */
  streamId?: string;
}

export class TurnAborts {
  private readonly turns = new Map<string, RunningTurn>();

  /** Registers a turn and returns the signal that aborts it. */
  start(streamId: string, chatId: string, userId: string): AbortSignal {
    const controller = new AbortController();
    this.turns.set(streamId, { chatId, userId, controller });
    return controller.signal;
  }

  finish(streamId: string): void {
    this.turns.delete(streamId);
  }

  /** Aborts the matching turns and returns how many it stopped. */
  stop({ chatId, userId, streamId }: StopRequest): number {
    let stopped = 0;
    for (const [id, turn] of this.turns) {
      const matches =
        turn.chatId === chatId &&
        turn.userId === userId &&
        (streamId === undefined || id === streamId);
      if (!matches) continue;
      turn.controller.abort();
      this.turns.delete(id);
      stopped++;
    }
    return stopped;
  }
}
