# Author a payload

> Power-user guide for adding a new **App payload** — the code + config that
> runs *inside* a deployed Databricks App, behind the generic App host. This
> documents the `AppPayloadAdapter` contract as it exists today
> ([`agent_foundry/deploy/payload_adapters.py`](../../agent_foundry/deploy/payload_adapters.py)).
>
> The bundled `agent` payload (agent runtime + chat UI) lives under
> [`payloads/agent/`](../../payloads/agent) and is the reference implementation.

## Payload vs. brick

A **brick** provisions infrastructure *around* the App (warehouses, Genie
spaces, vector indexes, …). A **payload** is what actually runs *in* the App
container — its runtime command, environment, and any deploy-time pre-steps.
The generic App host (`bricks/app/`) is payload-agnostic: it drives a fixed
deploy sequence and depends only on the `AppPayloadAdapter` structural type,
so it never learns a payload's internals.

Which payload an instance uses is selected by its manifest,
`instances/<name>/payloads/<payload>/payload.yml`:

```yaml
payload:
  adapter: agent          # -> selects the AppPayloadAdapter named "agent"
  requires: [warehouse, tracing, observability]  # framework bricks this payload needs
  spec: { ... }           # opaque to the framework; the adapter interprets it
```

`payload.requires` is framework-owned (not inside `spec`). Each name is a deploy brick. The brick's `applies_to` consults this list instead of hard-coding payload ids. A full deploy errors if a required brick is `--skip`'d or cannot run on the instance (for `tracing`: `mlflow` installed and `tracing_experiment` configured; for `warehouse`: a `warehouse/config.yml` or a reused `warehouse_id`). `--only` is a partial deploy and does not have to satisfy every requirement.

Example: the `rest_mcp` payload omits tracing, so a proxy instance never imports `mlflow`. An `agent` payload that listed `tracing` but ran in a venv without `mlflow` fails closed.

## Two ways to add a payload

1. **Built-in** (inside this framework repo): create `payloads/<name>/` and
   register the adapter class in
   [`agent_foundry/deploy/payload_adapters.py`](../../agent_foundry/deploy/payload_adapters.py)
   → `_builtin_adapters()`.
2. **Third-party** (no fork): ship a package that declares an entry point in
   the `agent_foundry.payload_adapters` group. The framework discovers it
   automatically — no framework edit, and its `scaffold` verbs light up in the
   CLI too:

   ```toml
   # in your package's pyproject.toml
   [project.entry-points."agent_foundry.payload_adapters"]
   rest_mcp = "my_package.adapter:RestMcpPayloadAdapter"
   ```

   A third-party adapter whose `name` matches a built-in **overrides** the
   built-in (the intended customisation path).

## The `AppPayloadAdapter` contract

Any class satisfying the `AppPayloadAdapter` protocol:

| Member | Purpose |
|--------|---------|
| `name: str` | Stable unique id; matched against `payload.adapter`. |
| `applies_to(instance_dir, config) -> bool` | Whether this adapter serves the instance (typically checks for its `payloads/<name>/` surface). |
| `build_payload_spec(instance_dir, variables, spec) -> AppPayloadSpec` | The runtime contract that runs *inside* the container (command / env / wheel extras). `spec` is the opaque `payload.spec` mapping. |
| `build_infra(instance_dir, variables, *, target) -> AppInfraSpec` | The neutral infrastructure the host provisions *around* the container (compute / resources / scopes / grants). |
| `after_reconcile(ws, app_name, variables, *, dry_run) -> None` | Optional heal hook between spec reconcile and code deploy (no-op if unused). |
| `provision(ctx) -> int` | Deploy-time pre-steps (e.g. Lakebase); may mutate `ctx.variables`; non-zero rc aborts the deploy. |
| `plan_destroy(ctx) -> list[DestroyPlanItem]` | Payload-owned teardown plan (shown before any API calls). |
| `destroy(ctx) -> list[BrickResult]` | Tear down payload-owned resources; idempotent. Report each deleted resource in `BrickResult.deleted` (see [Destroy waits until gone](author-a-brick.md#destroy-waits-until-gone)). |
| `load_state_outputs(ctx) -> dict[str, str]` | Payload-owned deploy variables to republish on (re)deploy. |
| `scaffolds() -> Sequence[ScaffoldDecl]` | Scaffold verbs this payload contributes to `agent-foundry scaffold` (see below). Return `()` for none. |

## Contribute scaffold verbs

A payload declares its authoring surfaces as
[`ScaffoldDecl`](../../agent_foundry/scaffold_spec.py) objects rather than the
framework hard-coding them. `agent-foundry scaffold` composes these with the
framework's own brick scaffolds, so `agent-foundry scaffold <your-verb>` and
`scaffold --list` pick them up automatically. Each `ScaffoldDecl` names a CLI
verb and the copy operations (`ScaffoldSource`) that materialise it:

```python
from pathlib import Path
from agent_foundry.scaffold_spec import ScaffoldDecl, ScaffoldSource

_PKG = Path(__file__).resolve().parent

def scaffolds(self):
    return (
        ScaffoldDecl(
            name="rest_mcp",                 # -> agent-foundry scaffold rest_mcp
            group="rest_mcp payload",        # heading in `scaffold --list`
            display="REST MCP Payload",
            summary="Starter manifest for the REST-backed MCP payload.",
            sources=(
                ScaffoldSource(
                    source=_PKG / "_scaffold" / "payload.yml",   # ships in your package
                    destination=f"payloads/{self.name}/payload.yml",  # instance-relative
                    shape="file",                                # or "tree" for a directory
                ),
            ),
        ),
    )
```

Scaffold names must be unique across all owners — a collision (e.g. a payload
declaring `warehouse`) fails fast with a clear error.

## Minimal skeleton

```python
from agent_foundry.deploy.app_host import AppInfraSpec, AppPayloadSpec
from agent_foundry.deploy.payload_adapters import AppPayloadAdapter


class RestMcpPayloadAdapter:
    name = "rest_mcp"

    def applies_to(self, instance_dir, config):
        return (instance_dir / "payloads" / self.name).is_dir()

    def build_payload_spec(self, instance_dir, variables, spec) -> AppPayloadSpec:
        ...

    def build_infra(self, instance_dir, variables, *, target) -> AppInfraSpec:
        ...

    def after_reconcile(self, ws, app_name, variables, *, dry_run):
        return None

    def provision(self, ctx):
        return 0

    def plan_destroy(self, ctx):
        return []

    def destroy(self, ctx):
        return []

    def load_state_outputs(self, ctx):
        return {}

    def scaffolds(self):
        return ()


# Module-level instance doubles as a static protocol check.
_: AppPayloadAdapter = RestMcpPayloadAdapter()
```

## Extend the agent payload

To add product-specific surface on top of the stock `agent` payload (a
tab, tiles, extra tools) without forking it, write a **wrapping adapter**
plus an **agent extension**. The instance keeps `payloads/agent/` (skills,
MCP servers, prompt) and only switches `adapter:`:

```yaml
payload:
  adapter: my_product        # wraps the agent adapter
  spec:
    runtime: { ... }         # stock agent spec, unchanged
    extensions:
      my_product: { ... }    # opaque to the agent; your adapter validates it
```

The wrapping adapter delegates every `AppPayloadAdapter` member to
`AgentPayloadAdapter`, validates `spec.extensions.<name>`, and appends two
env entries to the `AppPayloadSpec` it returns:

| Env var | Value | Effect |
|---|---|---|
| `AGENT_EXTENSIONS` | module path(s) exposing `EXTENSION` | The agent server loads them ([`payloads/agent/runtime/extensions.py`](../../payloads/agent/runtime/extensions.py)). |
| `CHAT_UI_OVERLAYS` | package name(s) | Each package's `ui/overrides/` is overlaid on the chat UI after the agent's own overrides. |

An `AgentExtension` provides:

- `proxy_paths()`: client-side routes the chat proxy must forward (otherwise a reload on them 404s);
- `register_routes(app)`: FastAPI routes on the agent server, inserted ahead of the chat proxy;
- `request_tools(context)`: tools for one request, given the conversation id, caller identity and forwarded headers.

The UI overlay replaces only `client/src/extensions/registry.tsx` to fill
the chat UI slots declared in `client/src/extensions/types.ts` (extra
routes, sidebar entries, new-chat suggestions, a per-chat effect, and a
`pinnedPanel` prop on `Chat`). It must not replace other agent overrides;
if a slot is missing, add it upstream.

## Layering rule

Payloads may import from the framework (`agent_foundry.*`); the framework must
**never** import from `payloads.*`. This one-way dependency is enforced by
[`tests/contracts/test_import_boundaries.py`](../../tests/contracts/test_import_boundaries.py). The
framework reaches payload code only through the `AppPayloadAdapter` seam, and
loads adapters lazily so no payload is imported at framework import time.

## Checklist

- [ ] Adapter implements every `AppPayloadAdapter` member (the module-level
      `_: AppPayloadAdapter = ...()` line is a free static check).
- [ ] Built-ins only: registered in
      [`agent_foundry/deploy/payload_adapters.py`](../../agent_foundry/deploy/payload_adapters.py)
      → `_builtin_adapters()`.
- [ ] Third-party: declares the `agent_foundry.payload_adapters` entry point.
- [ ] `scaffolds()` sources ship with your package and use unique verb names.
- [ ] No `payloads.* → agent_foundry.*`-violating imports the other way.

See [Architecture](../explanation/architecture.md) for how the App host and
payloads compose at deploy time, and [Author a brick](author-a-brick.md) for
the infrastructure side.
