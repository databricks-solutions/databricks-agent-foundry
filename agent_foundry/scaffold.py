"""Scaffold generation for ``agent-foundry scaffold <name>``.

This module is the *discovery surface* for the configurable shape of
every brick and payload.  Each scaffold writes a fully-commented
reference file (or tree) into ``<instance_dir>/`` so authors can see --
in their editor, with autocomplete and validation -- every option a
brick or payload actually accepts.

Design notes
------------

* **Declarative + composed (issue #368).** Every scaffold is a
  :class:`~agent_foundry.scaffold_spec.ScaffoldDecl` describing its CLI
  name, help copy, and the copy operations that materialise it.  The
  framework declares its own brick scaffolds here; each
  :class:`~agent_foundry.deploy.payload_adapters.AppPayloadAdapter`
  declares its scaffolds via ``scaffolds()``.  :func:`build_scaffold_registry`
  merges both, so a new payload adds ``scaffold`` verbs (its manifest,
  tool providers, ...) without touching this module.
* **Owner-owned content.** A brick reads its source from
  ``bricks/<brick>/_scaffold/``; a payload reads from inside its own
  package.  The framework only owns the generic renderer and the IO
  mechanics -- owner-specific content and destinations stay with the
  owner.
* **Reference, not minimum.** ``_scaffold/`` is the comprehensive
  reference for what a brick/payload lets you configure -- every field
  its typed config exposes appears with a comment.  CI enforces this;
  see ``tests/contracts/test_scaffold_completeness.py``.
* Bricks with no user-authored config (``observability``) are
  intentionally **not** scaffoldable: their knobs live elsewhere.
* Refuses to clobber existing files unless ``force=True``.
* Scaffolds are *just file IO* -- they never import a brick's deploy
  module or touch workspace state.  Safe to call against a partially-
  bootstrapped repo.

The agent payload owns its full authored surface under
``payloads/agent/`` (manifest, skills, mcp_servers); ``app/`` stays a
generic host shell (``config.yml`` only).  The agent's ``payload`` /
``skills`` / ``mcp`` scaffolds are contributed by its adapter, not
declared here (issue #338, #368).
"""

from __future__ import annotations

from pathlib import Path

from agent_foundry.bricks import ScaffoldResult
from agent_foundry.scaffold_spec import ScaffoldDecl, ScaffoldSource

# Filesystem layout root of the framework's bundled per-brick templates.
# Resolved at import time once -- ``Path(__file__).parents[1]`` is
# always the framework repo root (or installed wheel root); ``bricks/``
# ships alongside ``agent_foundry/`` in both layouts.
_FRAMEWORK_ROOT: Path = Path(__file__).resolve().parents[1]
_BRICKS_DIR: Path = _FRAMEWORK_ROOT / "bricks"

# Group label for the framework's own brick scaffolds in the grouped
# ``scaffold --list`` output.  Payload adapters supply their own group
# label (e.g. ``"agent payload"``).
_FRAMEWORK_GROUP = "framework"


# ---------------------------------------------------------------------------
# Framework brick scaffolds (declarative)
# ---------------------------------------------------------------------------
#
# One :class:`ScaffoldDecl` per scaffoldable framework brick.  Order here
# is the order framework scaffolds render in ``scaffold --list``: data
# layer first (warehouse, domains, functions), then evaluation, then the
# agent-side read surfaces (genie, vector search), then the deployed App
# host.
#
# Destination subdirs match the directory each brick's ``applies_to``
# checks in ``instance_dir`` -- NOT always identical to the brick's
# ``.name`` (e.g. ``sql_function`` reads from ``sql_functions/``).
# Writing to the brick-name dir instead would leave the next
# ``agent-foundry deploy`` silently skipping the brick.
#
# The generic ``app`` host scaffolds infra only (``config.yml``); the
# agent runtime manifest (``payload.yml``) is a payload-owned scaffold
# contributed by the agent adapter (issue #338).


def _framework_scaffolds() -> tuple[ScaffoldDecl, ...]:
    b = _BRICKS_DIR
    return (
        ScaffoldDecl(
            name="warehouse",
            group=_FRAMEWORK_GROUP,
            display="SQL Warehouse",
            summary="Provisions or reuses a serverless SQL warehouse the agent runs queries against.",
            sources=(ScaffoldSource(b / "warehouse" / "_scaffold" / "config.yml", "warehouse/config.yml", "file"),),
        ),
        ScaffoldDecl(
            name="data_domains",
            group=_FRAMEWORK_GROUP,
            display="Data Domains",
            summary="Curated UC views or tables — the agent's read surface.",
            sources=(ScaffoldSource(b / "data" / "_scaffold", "data_domains/example_domain", "tree"),),
        ),
        ScaffoldDecl(
            name="sql_function",
            group=_FRAMEWORK_GROUP,
            display="SQL Functions",
            summary="Custom UDFs the agent can call as tools (e.g. lookup_customer(id)).",
            sources=(ScaffoldSource(b / "sql_function" / "_scaffold", "sql_functions/example_functions", "tree"),),
        ),
        ScaffoldDecl(
            name="evaluation",
            group=_FRAMEWORK_GROUP,
            display="Evaluation",
            summary="MLflow GenAI eval config — runs your golden Q&A set against the deployed agent.",
            sources=(
                ScaffoldSource(
                    b / "evaluation" / "_scaffold" / "eval_config.yml", "evaluation/eval_config.yml", "file"
                ),
                ScaffoldSource(
                    b / "evaluation" / "_scaffold" / "datasets" / "smoke.yml",
                    "evaluation/datasets/smoke.yml",
                    "file",
                ),
            ),
        ),
        ScaffoldDecl(
            name="genie_spaces",
            group=_FRAMEWORK_GROUP,
            display="Genie Spaces",
            summary="Natural-language Q&A spaces over UC tables for stakeholder self-service.",
            sources=(ScaffoldSource(b / "genie" / "_scaffold", "genie_spaces/example_space", "tree"),),
        ),
        ScaffoldDecl(
            name="vector_indexes",
            group=_FRAMEWORK_GROUP,
            display="Vector Search",
            summary="Vector indexes for retrieval — embed a UC table, query by semantic similarity.",
            sources=(ScaffoldSource(b / "vector_search" / "_scaffold", "vector_indexes/example_index", "tree"),),
        ),
        ScaffoldDecl(
            name="model_serving",
            group=_FRAMEWORK_GROUP,
            display="Model Serving",
            summary="Serverless endpoint for a UC model (CPU or GPU), optionally built by a serverless job.",
            sources=(ScaffoldSource(b / "model_serving" / "_scaffold", "model_serving/example_model", "tree"),),
        ),
        ScaffoldDecl(
            name="jobs",
            group=_FRAMEWORK_GROUP,
            display="Jobs",
            summary=(
                "Databricks Jobs with notebook, Spark Python, or SQL tasks and schedule or file-arrival triggers."
            ),
            sources=(ScaffoldSource(b / "job" / "_scaffold", "jobs/example_job", "tree"),),
        ),
        ScaffoldDecl(
            name="ai_gateway",
            group=_FRAMEWORK_GROUP,
            display="Unity Gateway",
            summary="Git-managed Unity Catalog skills, deployed to a schema the agent pins or discovers.",
            sources=(ScaffoldSource(b / "ai_gateway" / "_scaffold", "ai_gateway", "tree"),),
        ),
        ScaffoldDecl(
            name="app",
            group=_FRAMEWORK_GROUP,
            display="App",
            summary=(
                "The Databricks App host: compute, resources and OBO scopes "
                "(infra only — the payload manifest is a separate scaffold)."
            ),
            sources=(ScaffoldSource(b / "app" / "_scaffold" / "config.yml", "app/config.yml", "file"),),
        ),
    )


# ---------------------------------------------------------------------------
# Registry composition (framework + payload adapters)
# ---------------------------------------------------------------------------


def _ordered_decls() -> list[ScaffoldDecl]:
    """Framework scaffolds followed by every payload adapter's scaffolds.

    Preserves declaration order (framework first, then adapters in
    registry order) so the grouped listing is deterministic.  Raises
    :class:`ValueError` on a duplicate ``name`` across owners -- two
    scaffolds answering the same verb is an authoring bug that would
    otherwise silently shadow one another.
    """
    # Imported lazily: ``iter_payload_adapters`` pulls in the payload
    # packages, which this framework module must not import at load time
    # (enforced by tests/contracts/test_import_boundaries.py).
    from agent_foundry.deploy.payload_adapters import iter_payload_adapters

    decls: list[ScaffoldDecl] = list(_framework_scaffolds())
    for adapter in iter_payload_adapters():
        decls.extend(adapter.scaffolds())

    seen: dict[str, str] = {}
    for decl in decls:
        if decl.name in seen:
            raise ValueError(
                f"Duplicate scaffold name {decl.name!r}: declared by both "
                f"{seen[decl.name]!r} and {decl.group!r}. Scaffold names must be unique."
            )
        seen[decl.name] = decl.group
    return decls


def build_scaffold_registry() -> dict[str, ScaffoldDecl]:
    """Return the composed ``name -> ScaffoldDecl`` registry.

    Built lazily on each call (cheap) so newly-installed payload adapters
    are picked up without re-importing the framework.
    """
    return {decl.name: decl for decl in _ordered_decls()}


def available_scaffold_names() -> list[str]:
    """Return the sorted list of scaffoldable names (bricks + payloads)."""
    return sorted(build_scaffold_registry())


def scaffold_entries() -> list[ScaffoldDecl]:
    """Return every scaffold in display order (framework first, then payloads).

    Ordered for presentation -- ``scaffold --list`` groups by
    :attr:`ScaffoldDecl.group` while preserving this order within a group.
    """
    return _ordered_decls()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _write(target: Path, body: str, *, force: bool, result: ScaffoldResult) -> None:
    """Write *body* to *target* unless the file already exists.

    Centralises the clobber-guard so every scaffold has identical
    overwrite semantics.  Mutates *result* in place: appends to
    ``written`` on a fresh write, to ``skipped`` when the target exists
    and *force* is False.
    """
    if target.exists() and not force:
        result.skipped.append(target)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    result.written.append(target)


def _render_file(source: Path, dst: Path, *, force: bool, result: ScaffoldResult) -> None:
    """Copy a single scaffold source file to *dst* (clobber-guarded)."""
    if not source.is_file():
        raise FileNotFoundError(
            f"Scaffold source {source} not found -- this is a framework packaging bug, not a user error."
        )
    _write(dst, source.read_text(encoding="utf-8"), force=force, result=result)


def _render_tree(source: Path, dst: Path, *, force: bool, result: ScaffoldResult) -> None:
    """Recursively copy every file under *source* into *dst* (clobber-guarded)."""
    if not source.exists():
        raise FileNotFoundError(
            f"Scaffold source {source} not found -- this is a framework packaging bug, not a user error."
        )
    for source_path in sorted(source.rglob("*")):
        if source_path.is_dir() or "__pycache__" in source_path.parts:
            continue
        rel = source_path.relative_to(source)
        _write(dst / rel, source_path.read_text(encoding="utf-8"), force=force, result=result)


def _render_decl(decl: ScaffoldDecl, instance_dir: Path, *, force: bool) -> ScaffoldResult:
    """Materialise every source of *decl* under *instance_dir*."""
    result = ScaffoldResult()
    for src in decl.sources:
        dst = instance_dir / src.destination
        if src.shape == "file":
            _render_file(src.source, dst, force=force, result=result)
        else:
            _render_tree(src.source, dst, force=force, result=result)
    return result


def run_scaffold(name: str, instance_dir: Path, *, force: bool = False) -> ScaffoldResult:
    """Dispatch to the named scaffold via the composed registry.

    Raises :class:`KeyError` for unknown names so the CLI can surface the
    available list.
    """
    registry = build_scaffold_registry()
    if name not in registry:
        raise KeyError(name)
    return _render_decl(registry[name], instance_dir, force=force)


# ---------------------------------------------------------------------------
# User-facing listing (CLI discoverability)
# ---------------------------------------------------------------------------


def format_scaffold_listing() -> str:
    """Return a human-readable, grouped table of every scaffoldable item.

    Grouped by :attr:`ScaffoldDecl.group` (framework bricks first, then
    each payload's contributions), each row showing the name the user
    types, a recognisable display label, and a one-line summary of what
    it *does*.  Used by ``scaffold --help``, ``scaffold --list``, and the
    "missing NAME" error message.  Plain text (no markup) so it renders
    identically in Typer's help formatter and on a bare ``print()``.
    """
    entries = scaffold_entries()
    if not entries:
        return ""
    name_w = max(len(e.name) for e in entries)
    label_w = max(len(e.display) for e in entries)

    lines: list[str] = []
    current_group: str | None = None
    for entry in entries:
        if entry.group != current_group:
            if lines:
                lines.append("")
            lines.append(f"{entry.group}:")
            current_group = entry.group
        lines.append(f"  {entry.name.ljust(name_w)}  {entry.display.ljust(label_w)}  {entry.summary}")
    return "\n".join(lines)
