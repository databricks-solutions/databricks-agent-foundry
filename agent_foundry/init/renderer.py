"""Render a generated repo skeleton from an :class:`InitContext`.

This module owns all file IO for ``agent-foundry init``.  The wizard
(:mod:`agent_foundry.init.wizard`) hands it a validated
:class:`InitContext`; the renderer turns that into a directory tree
on disk.  Keeping all writes here means tests can either drive the
full renderer against ``tmp_path`` (golden-file style) or drive the
individual template helpers in :mod:`agent_foundry.init.templates`
without any filesystem at all.

Refusal semantics
-----------------

The renderer refuses to write into a non-empty directory unless the
caller passes ``force=True``.  The wizard never sets ``force``;
forcing is a power-user escape hatch for the not-yet-built
``--non-interactive`` flow.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from agent_foundry.init import _templates as templates
from agent_foundry.init import ci_templates
from agent_foundry.init.examples_registry import (
    ExamplesRegistryError,
    Variant,
    get_variant,
)
from agent_foundry.init.wizard import InitContext

# Offline checks and deployment checks; the plugin marks tests/live/ ``live``.
_INSTANCE_TEST_DIRS = ("tests/unit", "tests/live")


class InitRenderError(RuntimeError):
    """Renderer-level failure (target directory not empty, variant
    tree missing, ...).

    Distinct exception type so the CLI verb (PR4) can map it to a
    user-friendly ``typer.Exit(code=...)`` without swallowing
    unrelated errors.
    """


def render(ctx: InitContext, outdir: Path, *, force: bool = False) -> None:
    """Materialise the generated repo under ``outdir``.

    Arguments:
        ctx: Validated wizard outputs.
        outdir: Destination directory.  Created if it does not
            exist.  Must be empty (or ``force=True``).
        force: When ``True``, the renderer writes into ``outdir``
            even if it contains files.  Existing files with the
            same name as a generated file ARE overwritten.

    Raises:
        InitRenderError: If ``outdir`` is non-empty and ``force`` is
            ``False``, or if the requested variant tree is missing
            from this build (non-blank starter only).
    """
    outdir = outdir.expanduser().resolve()
    _prepare_outdir(outdir, force=force)

    _write_text(outdir / "pyproject.toml", templates.render_pyproject(ctx))
    _write_text(outdir / "foundry.toml", templates.render_foundry_toml(ctx))
    _write_text(
        outdir / "foundry.toml.example",
        templates.render_foundry_toml_example(ctx),
    )
    _write_text(outdir / ".gitignore", templates.render_gitignore(ctx))
    _write_text(outdir / "README.md", templates.render_readme(ctx))
    # Pin the lock/CI Python to 3.11 (same as framework CI). requires-python
    # still allows >=3.11,<3.14; .python-version is the resolve pin.
    _write_text(outdir / ".python-version", "3.11\n")

    _write_schemas(outdir)
    _write_instance_content(ctx, outdir)
    _write_ci_files(ctx, outdir)


# ---------------------------------------------------------------------------
# Section helpers (top-level, no nested function defs)
# ---------------------------------------------------------------------------


def _prepare_outdir(outdir: Path, *, force: bool) -> None:
    """Create ``outdir`` if missing; refuse to clobber existing files.

    The wizard funnel always lands here with a fresh path (``./<name>/``
    where ``<name>`` was just collected), so a non-empty hit means
    the user re-ran init against a path they already used.  Without
    ``force`` we bail rather than silently merge.
    """
    if outdir.exists():
        if not outdir.is_dir():
            raise InitRenderError(f"{outdir} exists and is not a directory")
        if not force and any(outdir.iterdir()):
            raise InitRenderError(
                f"{outdir} is not empty -- refusing to overwrite. "
                "Re-run with --force, or pick a different project name."
            )
    else:
        outdir.mkdir(parents=True)


def _write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path``, ensuring parent dirs exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_schemas(outdir: Path) -> None:
    """Populate ``.agent-foundry/schemas/`` via the framework's
    generator.

    Re-uses ``agent_foundry.schemas.generate_all_schemas`` so the
    generated repo's IDE-validation surface is identical to what
    ``agent-foundry sync-schemas`` would produce in a hand-rolled
    repo.
    """
    from agent_foundry.schemas import generate_all_schemas

    schemas_dir = outdir / ".agent-foundry" / "schemas"
    schemas_dir.mkdir(parents=True, exist_ok=True)
    generate_all_schemas(out_dir=schemas_dir)


def _write_instance_content(ctx: InitContext, outdir: Path) -> None:
    """Write the per-instance directory tree according to the
    starter template.

    * ``blank`` -- only ``instances/<name>/foundry.toml`` with the
      identity fields the typed contract requires.
    * any other value -- treat the starter as an ``examples/<variant>/``
      id and copy that variant's tree from
      ``examples/<variant>/`` byte-for-byte into the instance
      directory, then rewrite the *identity* fields (``name``,
      ``schema_prefix``, ``description``) in the per-instance
      ``foundry.toml`` so they match the project name the user just
      chose.  We deliberately do NOT rewrite identity tokens inside
      per-brick files (skill prompts, SQL, YAML configs) -- the
      vendored content references ``${var.schema_prefix}`` rather
      than the literal value, so rewriting ``foundry.toml`` is
      enough for downstream variable substitution to pick up the
      new name.
    """
    instance_dir = outdir / "instances" / ctx.project_name
    instance_dir.mkdir(parents=True, exist_ok=True)

    if ctx.starter_template == "blank":
        _write_text(
            instance_dir / "foundry.toml",
            templates.render_instance_foundry_toml_blank(ctx),
        )
        for test_dir in _INSTANCE_TEST_DIRS:
            _write_text(instance_dir / test_dir / ".gitkeep", "")
        return

    _copy_variant(ctx.starter_template, instance_dir)
    _rewrite_instance_identity(instance_dir / "foundry.toml", ctx)
    _rewrite_payload_lakebase_hint(instance_dir / "payloads" / "agent" / "payload.yml", ctx)


def _rewrite_instance_identity(foundry_toml: Path, ctx: InitContext) -> None:
    """Rewrite the identity fields in a vendored per-instance
    ``foundry.toml`` to match ``ctx.project_name``.

    Touches three lines only:

    * ``name``           -- becomes ``ctx.project_name``.
    * ``schema_prefix``  -- becomes ``ctx.project_name`` with hyphens
      replaced by underscores (Unity Catalog identifier rule).
    * ``description``    -- replaced with a generic line so the
      portfolio-assistant copy does not advertise itself as that
      reference agent in e.g. the Databricks Apps UI.

    Everything else (warehouse mode, chat_history toggle,
    vs_endpoint_name, comments) is preserved verbatim because those
    are infrastructure choices the user inherits as opinionated
    defaults from the sample.

    Regex-based rewrite rather than a TOML round-trip because the
    Python stdlib ``tomllib`` is read-only and pulling in
    ``tomli-w`` to flip three lines would lose all the inline
    comments the vendored file relies on for first-time-user
    guidance.  The patterns are anchored to ``key = "value"`` form
    so a future TOML reformat that drops the quotes or moves the
    fields under a ``[section]`` would surface as a test failure.
    """
    if not foundry_toml.is_file():
        raise InitRenderError(
            f"variant is missing a foundry.toml at {foundry_toml} -- "
            "the variant tree is malformed; re-sync from the canonical "
            "sample-agent repo."
        )

    schema_prefix = ctx.project_name.replace("-", "_")
    try:
        variant = get_variant(ctx.starter_template)
    except ExamplesRegistryError as exc:
        raise InitRenderError(f"unknown starter_template {ctx.starter_template!r}: {exc}") from exc
    description = f"Agent generated by agent-foundry init from the {variant.manifest.display_name} example"

    content = foundry_toml.read_text(encoding="utf-8")
    content, n_name = re.subn(
        r'^name(\s*)=(\s*)"[^"]*"',
        f'name\\1=\\2"{ctx.project_name}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    content, n_prefix = re.subn(
        r'^schema_prefix(\s*)=(\s*)"[^"]*"',
        f'schema_prefix\\1=\\2"{schema_prefix}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    content, n_desc = re.subn(
        r'^description(\s*)=(\s*)"[^"]*"',
        f'description\\1=\\2"{description}"',
        content,
        count=1,
        flags=re.MULTILINE,
    )

    if n_name == 0 or n_prefix == 0:
        raise InitRenderError(
            "variant's foundry.toml does not have the expected "
            "``name`` / ``schema_prefix`` identity fields at the top "
            "level -- re-sync the variant from the canonical sample-agent "
            "repo and update _rewrite_instance_identity if the shape changed."
        )
    # ``description`` is optional in the typed contract; absence is
    # not an error but worth a note for the maintainer.
    del n_desc

    foundry_toml.write_text(content, encoding="utf-8")


def _rewrite_payload_lakebase_hint(payload_yml: Path, ctx: InitContext) -> None:
    """Rewrite the commented Lakebase ``project_name`` hint in payload.yml.

    The sample's ``spec.ui.chat_history`` block carries a commented-out
    ``# project_name: "portfolio-assistant-lakebase"`` hint.  Rewrite it
    to the user's project so the hint stays
    useful instead of advertising the sample.  A no-op when the file or
    hint is absent.
    """
    if not payload_yml.is_file():
        return
    content = payload_yml.read_text(encoding="utf-8")
    content = re.sub(
        r'# project_name:\s*"[^"]*-lakebase"',
        f'# project_name: "{ctx.project_name}-lakebase"',
        content,
        count=1,
    )
    payload_yml.write_text(content, encoding="utf-8")


def _copy_variant(variant_id: str, instance_dir: Path) -> None:
    """Copy ``examples/<variant_id>/`` into ``instance_dir`` byte-for-byte.

    Discovery goes through :mod:`agent_foundry.init.examples_registry`
    so the renderer and the wizard share one definition of "where do
    variants live and which ones exist".  Variant content lives under
    a Python package (``examples.<variant_id>``) so the wheel ships
    it via ``importlib.resources``; same mechanism the previous
    ``examples/<variant>/`` trees used before PR2.

    Skipped during the copy:

    * ``__init__.py`` / ``__pycache__`` -- Python packaging plumbing
      that exists only so :mod:`importlib.resources` can discover the
      tree.  Users would see a confusing ``__init__.py`` in their
      generated repo otherwise.
    * ``manifest.toml`` -- registry metadata about the variant, not
      part of the per-instance content.
    """
    try:
        variant: Variant = get_variant(variant_id)
    except ExamplesRegistryError as exc:
        raise InitRenderError(
            f"variant {variant_id!r} is missing from this build: {exc}. "
            "This usually means MANIFEST.in / package-data is out of "
            "sync with the examples/ directory.  Re-install "
            "agent-foundry from source or pin to a newer SHA."
        ) from exc

    if not variant.root.is_dir():
        raise InitRenderError(f"variant root {variant.root} is not a directory -- packaging is broken")

    skip = {"__init__.py", "__pycache__", "manifest.toml"}
    for child in variant.root.iterdir():
        if child.name in skip:
            continue
        target = instance_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)


def _write_ci_files(ctx: InitContext, outdir: Path) -> None:
    """Write the chosen provider's CI files.

    Only one provider's files land on disk per generated repo --
    writing both would force the user to delete one half on every
    init.
    """
    if ctx.ci_provider == "git":
        workflows_dir = outdir / ".github" / "workflows"
        for filename, content in ci_templates.render_github_workflows(ctx).items():
            _write_text(workflows_dir / filename, content)
    elif ctx.ci_provider == "gitlab":
        _write_text(outdir / ".gitlab-ci.yml", ci_templates.render_gitlab_ci(ctx))
    else:  # pragma: no cover - guarded by the InitContext literal type
        raise InitRenderError(
            f"unknown ci_provider {ctx.ci_provider!r} (wizard validation should "
            "have rejected this; please file a bug against agent-foundry)",
        )


__all__ = ["InitRenderError", "render"]
