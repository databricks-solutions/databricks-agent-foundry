// Loaded with `node --import`: points the upstream chat UI modules that the
// overrides import, but that only exist in the cloned template, at small
// stand-ins in this directory, so suites run without an upstream checkout.
import { registerHooks } from 'node:module';

const here = new URL('./', import.meta.url);
const overrides = new URL('../../../overrides/', import.meta.url);

const PACKAGE_STUBS = new Map([
  ['@chat-template/core/errors', new URL('chat-sdk-error.ts', here).href],
  [
    '@chat-template/core/chat-socket-protocol',
    new URL('packages/core/src/chat-socket-protocol.ts', overrides).href,
  ],
]);

const RELATIVE_STUBS = new Map([
  [
    new URL('client/src/lib/ChatTransport', overrides).pathname,
    new URL('chat-transport.ts', here).href,
  ],
]);

registerHooks({
  resolve(specifier, context, nextResolve) {
    const stub =
      PACKAGE_STUBS.get(specifier) ??
      (context.parentURL && specifier.startsWith('.')
        ? RELATIVE_STUBS.get(new URL(specifier, context.parentURL).pathname)
        : undefined);
    if (stub) {
      return { url: stub, shortCircuit: true };
    }
    // The upstream build resolves extensionless relative imports; Node's ESM loader needs the `.ts`.
    const extensionless = specifier.startsWith('.') && !/\.[cm]?[jt]sx?$/.test(specifier);
    return nextResolve(extensionless ? `${specifier}.ts` : specifier, context);
  },
});
