// Stand-in for upstream's packages/core/src/errors.ts: the fields the
// overrides read, without the message and status tables.
export type ErrorCode = `${string}:${string}`;

export class ChatSDKError extends Error {
  readonly type: string;
  readonly surface: string;
  override readonly cause?: string;

  constructor(errorCode: ErrorCode, cause?: string) {
    super(errorCode);
    const [type, surface] = errorCode.split(':');
    this.type = type;
    this.surface = surface;
    this.cause = cause;
  }
}
