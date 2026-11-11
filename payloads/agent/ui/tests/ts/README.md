# TypeScript unit tests for the agent payload UI overrides

This directory holds standalone `node:test` suites for the TypeScript
helpers that `payloads/agent/ui/overrides/server/src/lib/` ships into the
upstream `e2e-chatbot-app-next` chat UI at runtime.

## Why it lives here, not under `overrides/`

`_apply_overrides()` in
[`bricks/app/scripts/start_app.py`](../../../../../bricks/app/scripts/start_app.py)
rsync-copies the **entire** `payloads/agent/ui/overrides/` tree onto the
cloned upstream checkout at startup. Any `package.json`, `node_modules/`,
or `.test.ts` file placed under `overrides/` would be overlaid into the
upstream tree -- and a stray `server/package.json` will replace upstream's
`@databricks/chatbot-server` workspace declaration and crash the deployed
app at build time (regression caught after a deploy crashed
`dev-fancy-agent-app`; see the "Hard rule" section in
[`overrides/MANIFEST.md`](../../overrides/MANIFEST.md)).

Test files therefore live *outside* `overrides/` and import the helpers
under test via relative paths, e.g.:

```ts
import { sanitizeToolPairs } from '../../overrides/server/src/lib/sanitize-tool-pairs.ts';
```

An override that imports an upstream module which only exists in the
cloned template (`@chat-template/core/errors`, `client/src/lib/ChatTransport`)
gets a small stand-in from [`support/`](support/): `npm test` loads
`support/upstream-stubs.mjs`, a resolve hook that maps those imports to it.
Type-only imports (for example from `ai`) are erased and need nothing. The
hook also adds `.ts` to extensionless relative imports between overrides,
which the upstream build resolves on its own.

## Running the tests

Requires Node >= 22.15 (for `--experimental-strip-types` and
`module.registerHooks`). On Node 25 type stripping is on by default. No
`tsx` or compile step needed.

```bash
cd payloads/agent/ui/tests/ts
npm install
npm test
```

CI runs `npm test` in the `Offline tests` job on every PR. `npm install`
is only for editor types (`@types/node`); the tests have no runtime
dependencies.

### Behind a corporate firewall

If public npm is blocked on your network, point npm at your organization's
registry mirror:

```bash
npm install --registry=https://npm-mirror.example.com/
# or set NPM_CONFIG_REGISTRY in your shell
```

`package-lock.json` is intentionally not committed (see `.gitignore`)
because npm bakes the install-time registry URL into every `resolved`
field, which would break installs in downstream forks that don't have
access to the internal proxy.

## What goes here

Anything that:

- Tests a TS helper from `payloads/agent/ui/overrides/`.
- Is pure / does not need the chat UI to actually be running.
- Is small enough that a `node:test` suite is appropriate (no DOM, no
  server boot, no bundling).

For end-to-end tests that need the full chat UI booted, use the existing
Python harness under `bricks/app/tests/unit/` and `payloads/agent/tests/unit/runtime/`.
