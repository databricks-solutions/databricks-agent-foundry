# End-to-end init examples

Named **variants** under this directory are self-contained per-instance trees
that `agent-foundry init` can copy into a freshly generated application repo.
Each variant is an end-to-end solution (not a minimal brick starter — those
live under `bricks/<brick>/_scaffold/`).

## Shipped variants

| Id | Display name | Init menu |
| --- | --- | --- |
| `portfolio_assistant` | Portfolio Assistant | Option `2` after `blank` |
| `unity_gateway_assistant` | Unity Gateway Assistant | Listed by the init wizard (needs the Unity AI Gateway previews) |

## Adding a new variant

1. Create `examples/<variant_id>/` with the same shape as an existing variant
   (`foundry.toml`, per-brick subdirs, optional `tests/`).
2. Add `examples/<variant_id>/__init__.py` (empty package marker for wheel
   packaging).
3. Add `examples/<variant_id>/manifest.toml`:

   ```toml
   display_name = "My Example"
   description = "One-line summary shown in the init wizard"
   ```

4. Run `uv run pytest tests/contracts/test_examples_packaging.py agent_foundry/tests/unit/init/test_examples_registry.py` —
   CI asserts the variant is on disk with `__init__.py` + `manifest.toml` and that
   `pyproject.toml` already ships all of `examples/` via the parent
   `[tool.setuptools.package-data]` entry (no per-variant TOML edit required).

Do **not** commit `skills/_assembled/` under a variant; that directory is
produced at deploy time by the skills brick.

## Maintaining `portfolio_assistant`

The tree mirrors the `instances/portfolio-assistant/` layout of the
`agent-foundry-sample-agent` downstream repo. That repo may be consumed either
by pinning the public upstream or by pinning your own fork -- both are valid
(see the [README bootstrap options](../README.md#2-bootstrap-a-new-agent-repo)).
After syncing, run the init/renderer tests to confirm identity rewrite and
wheel packaging still pass.
