# Contributing

This repo is the **public pin target** for Agent Foundry (`bricks/` + `agent_foundry/`). It is published at [`databricks-solutions/databricks-agent-foundry`](https://github.com/databricks-solutions/databricks-agent-foundry).

Downstream agent applications -- your actual deployed agent with its `instances/<app>/` tree, customer-specific data, CI secrets, etc. -- live in **their own repos**, bootstrapped via `agent-foundry init` (see [README.md §Bootstrap a new agent repo](README.md#2-bootstrap-a-new-agent-repo)). Contribute to those repos directly; this guide is for contributing to the framework itself.

## Local development

```bash
git clone git@github.com:databricks-solutions/databricks-agent-foundry.git
cd databricks-agent-foundry
uv sync --frozen --extra all
uv run --no-sync pytest
```

`uv sync --frozen --extra all` pulls in every optional extra (`dev`, `runtime`, `eval`, `call`, `extraction`, `rest_mcp`); use a narrower set if you only need part of the test suite (`--extra dev` for offline-only). The committed `.python-version` pins **3.11** for lock/CI resolve (`requires-python` still allows `>=3.11,<3.14`). After `uv lock`, regenerate App pins with `python -m agent_foundry.constraints.export` so `agent_foundry/constraints/*.txt` stay in sync (CI asserts this). Dependabot pull requests that change `pyproject.toml` or `uv.lock` regenerate and commit these files automatically through the trusted default-branch exporter. Framework GitHub Actions and local installs use `uv sync --frozen` / `uv run --no-sync` so they consume the exact committed lock.

### Editor schemas (one-time setup)

Scaffolds and examples carry `# yaml-language-server: $schema=../../../.agent-foundry/schemas/...` headers that resolve against a local `.agent-foundry/schemas/` directory (the same convention downstream repos use). Run once after cloning so your editor's YAML/TOML language server can resolve them:

```bash
uv run agent-foundry sync-schemas
```

The generated `.agent-foundry/` directory is git-ignored on purpose -- the authoritative bytes live under [`agent_foundry/schemas/`](agent_foundry/schemas/) and are regenerated from the Pydantic models via `python -m agent_foundry.schemas._generate`. Re-run `sync-schemas` after pulling changes that touch those models.

## Branch + PR flow

| Branch | Rooted on | PR target |
|---|---|---|
| `fix/<slug>`, `feat/<slug>`, `docs/<slug>` | `origin/main` | `origin/main` |
| `test/<slug>`, `integration/<slug>` | anywhere | *(never pushed)* |

```bash
git switch main && git pull
git switch -c fix/my-thing
# edit, commit, push
git push -u origin HEAD
gh pr create --base main
```

CI (`ci.yml`) runs static checks, offline tests (plus offline `agent-foundry validate`), and the packaged example smoke on every PR; these must pass before merge. The Framework acceptance canaries (brick, payload, example, plus Agent serving live) run on PRs into `release`; each owner's live checks sit in its `tests/live/` folder. See [tests/README.md §What runs when](tests/README.md#what-runs-when).

## Public vs internal API

The `agent_foundry` package declares its public surface in [`agent_foundry/__init__.py`](agent_foundry/__init__.py) via `__all__`. Anything re-exported there is the documented public API and follows SemVer once the package is published as a library. Everything else -- submodule helpers, private functions, brick internals -- is implementation detail and may change without notice.

When you need a framework symbol:

- Prefer `from agent_foundry import InstanceConfig` over `from agent_foundry.config import InstanceConfig`, unless the symbol is intentionally kept private.
- If you find yourself reaching into a submodule for something that isn't public, either (a) make the case for adding it to `__all__` (and do so in the same PR), or (b) treat the coupling as tech debt worth flagging.
- Changing or removing a name in `__all__` is a breaking change and requires a CHANGELOG entry, a deprecation cycle on the old name, and a PR. The tripwire test in [`agent_foundry/tests/unit/test_public_api.py`](agent_foundry/tests/unit/test_public_api.py) will fail loudly if you try to slip one in silently.

## Runtime code never imports deploy code

The framework enforces a one-way dependency inside `bricks/`:

- **Runtime code** runs inside the deployed Databricks App container at request time. It lives under [`payloads/agent/runtime/`](payloads/agent/runtime/) and the non-`scripts/` parts of [`bricks/app/`](bricks/app/).
- **Deploy code** is every `bricks/*/scripts/*.py`, every `bricks/<name>/brick.py` adapter, the assembler, the validators, the `agent_foundry.deploy` package, the `agent_foundry.cli` subpackage, and `agent_foundry.validate`. It runs only at deploy time from the orchestrator or the `agent-foundry` CLI.

Runtime modules **must not** import deploy modules. The reason: a future split into a separate `agent-foundry-runtime` wheel requires the two layers to be independent today.

The rule is enforced by [`tests/contracts/test_import_boundaries.py`](tests/contracts/test_import_boundaries.py), an AST-walking test that fails CI if a runtime module contains an `import` or `from ... import` statement targeting `bricks.*.scripts`, `agent_foundry.deploy`, or one of the deploy-only `brick.py` adapters. When a runtime module needs something the deploy step produces (e.g. the assembled skill prompt), the deploy step writes it to disk under `<instance>/skills/_assembled/` and the runtime reads those files.

If the test starts failing after you add an import, either move the logic you need out of deploy code (usually into `agent_foundry.*`) or have the deploy step serialise its result to a file the runtime can read.

## Lazy imports for `[runtime]`-only dependencies

The same test enforces the symmetric rule: deploy / eval / call code **must not** top-level-import a package that exists only in the `[runtime]` extra. The runtime extra (`agents`, `slowapi`, `databricks_openai`, `databricks_ai_bridge`, `mcp`) is not installed on machines that only run `agent-foundry deploy` or the eval harness; a top-level import would raise `ModuleNotFoundError` before the verb reaches its handler.

The rule:

> Any symbol from a `[runtime]`-only dependency must be imported inside the function or class that uses it (the lazy pattern), or the importing module must live in the runtime slice ([`payloads/agent/runtime/`](payloads/agent/runtime/) or [`bricks/app/`](bricks/app/), minus their `scripts/` / `tests/` / `docs/` subdirs and the documented `brick.py` adapters).

Pinned by the same [`tests/contracts/test_import_boundaries.py`](tests/contracts/test_import_boundaries.py) file under `RUNTIME_ONLY_TOP_LEVEL_PACKAGES`. When a new `[runtime]`-only package is added to `pyproject.toml`, append its top-level import name there.

Why this matters: the deploy-time CLI (`agent-foundry deploy`, `agent-foundry validate`, the eval harness, `agent-foundry call`) is expected to work with `pip install agent-foundry[dev]` -- without the runtime extra. A stray top-level `from agents import ...` in `agent_foundry/deploy/` breaks that contract silently.

## `agent_foundry.contracts` (typed YAML contracts)

Typed Pydantic models for tool manifests, skill / domain metadata, and agent config YAMLs live under [`agent_foundry/contracts/`](agent_foundry/contracts/) and are re-exported at the top level (e.g. `from agent_foundry import ToolManifest`). They mirror the on-disk YAML shapes -- not the runtime-merged views -- so they can be used for validation without pulling in framework state.

These contracts are strictly additive today: no existing brick depends on them yet.

The long-term direction is to move away from YAML as the source of truth in favour of typed config, so YAML↔contract parity is no longer enforced by a framework-level test. Downstream repos that still load YAML should validate their own YAMLs against these models as part of their own CI.

## Brick registry consistency

Two registries describe the brick set:

- **Deploy registry** -- [`agent_foundry/bricks.py:iter_bricks`](agent_foundry/bricks.py). Ordered list of `Brick` instances the orchestrator runs at `agent-foundry deploy` time. Keys carry historical names (`data_domains`, `genie_spaces`, `vector_indexes`).
- **CLI dispatch table** -- [`agent_foundry/deploy/_brick_registry.py:BRICK_ACTIONS`](agent_foundry/deploy/_brick_registry.py). Maps `agent-foundry brick <name> <action>` to the script's `main(argv)`. Keys are the directory / CLI name (`data`, `genie`, `vector_search`).

The drift between the two name spaces is documented in [`DEPLOY_TO_CLI_NAME`](agent_foundry/deploy/_brick_registry.py). [`tests/contracts/test_registry_consistency.py`](tests/contracts/test_registry_consistency.py) asserts (a) every deploy brick has a reachable CLI entry once translated through `cli_name_for(...)`, (b) the drift map has no stale or wrong entries, (c) every CLI-only brick is documented in the test's `CLI_ONLY_BRICKS` set so adding a new one forces maintainer awareness.

When you rename a brick to align the two namespaces, drop the entry from `DEPLOY_TO_CLI_NAME` in the same PR; the test will fail until the drift is removed *or* re-recorded against the new name.

## Brick script convention (`bricks/<name>/scripts/<action>.py`)

Every concrete brick script (anything under `bricks/<name>/scripts/` that is not `__init__.py` or a documented stub) **must** expose:

```python
def main(argv: list[str] | None = None) -> int: ...
```

This is the contract the CLI dispatcher ([`agent_foundry/deploy/_brick_registry.py`](agent_foundry/deploy/_brick_registry.py)`:BRICK_ACTIONS`) relies on when forwarding `agent-foundry brick <name> <action>` argv to the script. The convention is pinned by [`tests/contracts/test_brick_script_convention.py`](tests/contracts/test_brick_script_convention.py) so a future change cannot silently break dispatch.

Recommended shape inside `main`:

1. Build an `argparse.ArgumentParser`.
2. Call [`agent_foundry.variables.add_project_root_arg`](agent_foundry/variables.py) to register the standard `--project-root` flag.
3. Parse `argv`, resolve paths and variables, and forward to a typed library function (`def deploy(...) -> int` or similar) that does the actual work.

The orchestrator ([`agent_foundry/deploy/_orchestrator.py`](agent_foundry/deploy/_orchestrator.py)) calls the typed `Brick.deploy(ctx)` protocol method directly -- not the script's `main(argv)` -- so the library/CLI split is real, not ceremonial. Keep `main` thin; put logic in the library function.

## Adding a CLI subcommand

The `agent-foundry` CLI is a thin typer wrapper over library functions and per-brick scripts. To add a new verb:

1. **Wire it in [`agent_foundry/deploy/cli/`](agent_foundry/deploy/cli/).** Each command group has its own module. A verb is a 5-15 line function (or a sub-`Typer` for a group) that parses flags, calls a library function, and re-raises the return code via `typer.Exit`. The module's `register(app)` adds it, and [`cli/__init__.py`](agent_foundry/deploy/cli/__init__.py) calls each `register` in the order `--help` lists them. Keep the body tiny -- if you find yourself adding logic, it belongs in `agent_foundry/...` or the target brick script, not in the CLI module.

2. **Add a smoke test in [`agent_foundry/tests/unit/test_cli.py`](agent_foundry/tests/unit/test_cli.py).** At minimum a `--help` smoke (parametrize the existing `test_help_smoke` list) and a happy-path invocation that mocks the underlying library function via `monkeypatch.setattr(...)`.

3. **Document it.** Add a row to the verb-summary table in [`README.md`](README.md) and a usage example in the matching subsection of [`docs/reference/cli.md`](docs/reference/cli.md) (Deployment, Local Development, Evaluation, ...).

For brick-specific entry points, prefer adding the action to [`agent_foundry/deploy/_brick_registry.py:BRICK_ACTIONS`](agent_foundry/deploy/_brick_registry.py) so it shows up under `agent-foundry brick <name> <action>` automatically; only promote to a first-class verb when the action is high-traffic enough to deserve its own help entry (e.g. `agent-foundry genie import`).

## Init examples (`examples/`)

End-to-end variants for `agent-foundry init` live under [`examples/<variant_id>/`](examples/). Each variant ships a `manifest.toml` (wizard menu text) and a per-instance tree that the renderer copies into `instances/<project_name>/` in the generated repo.

- **Adding a variant** -- see [`examples/README.md`](examples/README.md). No wizard/renderer code changes; wheel packaging uses a single parent `[tool.setuptools.package-data]` entry for `examples` (enforced by [`tests/contracts/test_examples_packaging.py`](tests/contracts/test_examples_packaging.py)).
- **Editing `portfolio_assistant`** -- the reference instance ([`examples/portfolio_assistant/`](examples/portfolio_assistant/)) is the in-tree source of truth. Edit it directly and run `uv run pytest agent_foundry/tests/unit/init`.
- **Do not commit** `skills/_assembled/` under a variant (deploy-time artifact).

Brick configuration reference for `agent-foundry scaffold` lives under `bricks/<brick>/_scaffold/`, not here.
