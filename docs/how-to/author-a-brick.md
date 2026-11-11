# Author a brick

> Power-user guide for adding a new deployable **brick** — a self-contained
> module the orchestrator discovers and deploys. This documents the `Brick`
> contract as it exists today (`agent_foundry/bricks.py`).
>
> Forward note: a planned engine refactor will split `Brick` into separate
> IaC / Runtime / Action facets. Until that lands, the single `Brick` protocol
> below is the contract.

## Two ways to add a brick

1. **Built-in** (inside this framework repo): create `bricks/<name>/` and
   register the class in `agent_foundry/bricks.py` → `_builtin_bricks()`.
2. **Third-party** (no fork): ship a package that declares an entry point in
   the `agent_foundry.bricks` group. The framework discovers it automatically:

   ```toml
   # in your package's pyproject.toml
   [project.entry-points."agent_foundry.bricks"]
   my_brick = "my_package.bricks:MyCustomBrick"
   ```

## The `Brick` contract

A brick is any class satisfying the `Brick` protocol
([`agent_foundry/bricks.py`](../../agent_foundry/bricks.py)):

| Member | Purpose |
|--------|---------|
| `name: str` | Stable unique id (logging, `--skip`/`--only`, dedup). |
| `deploy_order: int` | Ordering key. Built-ins use multiples of 10; third parties slot between (e.g. `25` runs after `20`, before `30`). Ties broken by `name`. |
| `display_name: str` | Human label in CLI output. |
| `state_schema: type[BaseModel] \| None` | Optional Pydantic model for the brick's workspace state file; `None` if it persists no state. |
| `applies_to(ctx) -> bool` | Should this brick run for the instance? Typically checks for its sub-directory. |
| `deploy(ctx) -> list[BrickResult]` | Do the work; one result per sub-instance. |
| `load_state_outputs(ctx) -> dict[str, str]` | Deploy-time outputs to expose downstream as `${var.*}`. |
| `plan_destroy(ctx) -> DestroyPlan` | What teardown *would* remove (shown before any API calls). |
| `destroy(ctx) -> list[BrickResult]` | Tear down, consulting persisted state; idempotent (`skipped` when nothing to do). |

`deploy()` and `destroy()` receive a `DeployContext` / `DestroyContext` with
`instance_dir`, `target`, `variables`, `dry_run`, and (when the orchestrator
provides them) `ws` (WorkspaceClient) and `config`. Pass `ctx.ws` into brick
scripts (`ws=ctx.ws`). Operator `--dry-run` may still set `ws` so preview can
read live state; `--offline` and unit tests leave `ws` None. Deploy/destroy
functions must not construct a client when `ws` is None. Standalone script
`main()` may call `prefer_workspace_client(variables)` once and pass `ws=`
into `deploy()`. Unit tests inject `ws=None` or an in-memory fake; they
must not `delenv` `AGENT_FOUNDRY_SKIP_DEFAULT_WORKSPACE_CLIENT` except in
tests of the factory itself. That env var is a CI tripwire, not the
offline API.

### Convenience base classes

- `StandardLayoutBrick` implements `load_state_outputs` for the two standard
  state layouts — set `state_subdir`, `state_keys`, and `is_nested_layout`
  instead of writing the loader yourself.
- `BrickDestroyDefaults` supplies no-op `plan_destroy` / `destroy` for bricks
  that don't manage teardown yet.

## Minimal skeleton

Modeled on `bricks/warehouse/brick.py`
([source](../../bricks/warehouse/brick.py)):

```python
from agent_foundry.bricks import (
    Brick,
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlan,
    StandardLayoutBrick,
)


class MyBrick(StandardLayoutBrick):
    name = "my_brick"
    display_name = "My Brick"
    deploy_order = 25
    state_schema = None

    # Flat layout: workspace <instance>/_state/my_brick/<instance>/<target>.json -> ${var.my_resource_id}
    state_subdir = "my_brick"
    state_keys = {"resource_id": "my_resource_id"}
    is_nested_layout = False

    def applies_to(self, ctx: DeployContext) -> bool:
        return (ctx.instance_dir / "my_brick").is_dir()

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:
        # delegate to bricks/my_brick/scripts/<script>.py:main(...)
        rc = 0
        return [BrickResult(name=ctx.instance_dir.name, status="ok" if rc == 0 else "deploy_error", rc=rc)]

    def plan_destroy(self, ctx: DestroyContext) -> DestroyPlan:
        return DestroyPlan()  # or list DestroyPlanItem(s)

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        return [BrickResult(name=ctx.instance_dir.name, status="skipped")]


# Module-level instance doubles as a static protocol check.
_: Brick = MyBrick()
```

## Destroy waits until gone

`destroy` reports what it deleted in `BrickResult.deleted`; the
orchestrator then polls each resource until it is gone and turns the
result into `destroy_error` if it outlives its timeout (5 min, 15 min for
vector indexes, Apps and Lakebase projects). Use `deleted_refs`, which
returns nothing on dry-run or a non-zero rc:

```python
from agent_foundry.bricks import deleted_refs

rc = delete_my_resource(...)
return [
    BrickResult(
        name=ctx.instance_dir.name,
        status="ok" if rc == 0 else "destroy_error",
        rc=rc,
        deleted=deleted_refs(ctx, rc, "job", job_id),
    )
]
```

Only report resources the call actually deleted (not ones skipped for
`--force` or missing state). Supported kinds are `ResourceKind` in
`agent_foundry.bricks`; a new kind needs a check in
`agent_foundry/deploy/gone.py`.

## Directory layout

```
bricks/my_brick/
  __init__.py        # REQUIRED to ship in the wheel (and to satisfy the doc-coverage guard)
  brick.py           # the Brick implementation
  scripts/           # main(argv) entrypoints that brick.deploy()/destroy() delegate to
  _scaffold/         # starter config copied by `agent-foundry scaffold my_brick`
  README.md          # the one authored doc (REQUIRED -- enforced by the coverage guard)
  tests/
```

## Wire the CLI escape hatch

To expose `agent-foundry brick my_brick <action>`, add the brick's actions to
`BRICK_ACTIONS` in `agent_foundry/deploy/_brick_registry.py`, mapping each
action to a `bricks.my_brick.scripts.<module>:main` target.

## Checklist (what the drift guards enforce)

- [ ] `bricks/my_brick/__init__.py` exists (ships in the wheel).
- [ ] `bricks/my_brick/README.md` exists — `tests/contracts/test_brick_doc_coverage.py` fails otherwise.
- [ ] Listed in both [`docs/index.md`](../index.md) and
      [`docs/reference/bricks.md`](../reference/bricks.md) — the brick-index
      guard requires the tables to match the wheel bricks exactly.
- [ ] Built-ins only: registered in `agent_foundry/bricks.py` → `_builtin_bricks()`.

See [Architecture](../explanation/architecture.md) for how bricks compose and
deploy in dependency order.
