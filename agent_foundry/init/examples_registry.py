"""Variant discovery for ``agent-foundry init``.

The framework ships one or more end-to-end app *variants* under the
top-level ``examples/`` directory (see :mod:`examples`).  Each variant
is a self-contained per-instance tree plus a ``manifest.toml`` that
declares its display name and one-line description.

This module reads those manifests and exposes them to the renderer
and the wizard.  It deliberately does **not** know what bricks a
variant uses, what files it ships, or how to render it -- those are
the renderer's concerns.  The registry's only job is "what variants
exist, and what should the wizard show for each?".

Adding a new variant requires no changes here: drop a directory under
``examples/<variant>/`` with a ``manifest.toml`` and the registry
picks it up on the next ``init`` invocation.
"""

from __future__ import annotations

import tomllib
from importlib import resources as _resources
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

_EXAMPLES_PACKAGE: Final[str] = "examples"
"""Importable package path of the variants directory.

Resolved via :func:`importlib.resources.files` so discovery works in
both source checkouts and wheel installs.
"""

_MANIFEST_FILENAME: Final[str] = "manifest.toml"


class VariantManifest(BaseModel):
    """Typed view of one ``examples/<variant>/manifest.toml``.

    Strict (``extra="forbid"``) so a typo in the manifest fails loudly
    rather than silently dropping the field; future additions need an
    explicit model change.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class Variant(BaseModel):
    """One discovered variant: identity + parsed manifest.

    ``id`` is the directory name under ``examples/`` (e.g.
    ``portfolio_assistant``).  ``root`` is the concrete filesystem
    path to that directory -- materialised via
    :func:`importlib.resources.as_file` so callers can ``shutil.copy``
    the contents without caring whether the framework is installed
    from source or from a wheel.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    id: str
    root: Path
    manifest: VariantManifest


class ExamplesRegistryError(RuntimeError):
    """Registry-level failure: malformed manifest, missing package, etc.

    Distinct exception type so the wizard / renderer can surface a
    user-friendly error without swallowing unrelated failures.
    """


def list_variants() -> list[Variant]:
    """Return every discoverable variant, sorted by id.

    A variant is "discoverable" iff ``examples/<id>/manifest.toml``
    exists AND parses as a valid :class:`VariantManifest`.  Directories
    without a manifest are silently skipped (e.g. ``__pycache__``,
    ``__init__.py``); a directory with a *malformed* manifest raises
    :class:`ExamplesRegistryError` so authoring mistakes surface
    immediately rather than silently disappearing from the menu.
    """
    try:
        examples_root = _resources.files(_EXAMPLES_PACKAGE)
    except (ModuleNotFoundError, FileNotFoundError) as exc:
        raise ExamplesRegistryError(
            f"examples/ package not found: {exc}.  This usually means "
            "the framework wheel was built without the examples tree "
            "in package-data; re-install agent-foundry from source or "
            "pin to a newer SHA."
        ) from exc

    with _resources.as_file(examples_root) as concrete_root:
        if not concrete_root.is_dir():
            raise ExamplesRegistryError(
                f"examples/ resolved to {concrete_root} which is not a directory -- packaging is broken"
            )
        return _scan_variants_dir(concrete_root)


def list_starter_options() -> list[tuple[str, str]]:
    """Return ``(value, help_text)`` pairs for the init wizard starter menu.

    Always begins with ``blank``.  Variant rows use the directory id
    as ``value`` (what :class:`InitContext.starter_template` stores)
    and ``display_name — description`` as help text so the numbered
    menu is self-explanatory without reading ``manifest.toml``.
    """
    options: list[tuple[str, str]] = [
        ("blank", "empty instance, add bricks later"),
    ]
    for variant in list_variants():
        manifest = variant.manifest
        options.append(
            (
                variant.id,
                f"{manifest.display_name} — {manifest.description}",
            )
        )
    return options


def allowed_starter_templates() -> list[str]:
    """All valid ``starter_template`` values (``blank`` plus variant ids)."""
    return [value for value, _ in list_starter_options()]


def get_variant(variant_id: str) -> Variant:
    """Return one variant by id, raising if it does not exist.

    Looking up a single variant goes through :func:`list_variants` so
    the discovery path is exercised identically in both call sites
    (no second filesystem-walk implementation to drift).
    """
    for variant in list_variants():
        if variant.id == variant_id:
            return variant
    available = [v.id for v in list_variants()]
    raise ExamplesRegistryError(f"unknown variant {variant_id!r}; available: {sorted(available)}")


# ---------------------------------------------------------------------------
# Internal helpers (top-level: no nested function defs per repo style)
# ---------------------------------------------------------------------------


def _scan_variants_dir(examples_root: Path) -> list[Variant]:
    """Walk ``examples_root`` and return every directory with a valid manifest."""
    variants: list[Variant] = []
    for child in sorted(examples_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("_") or child.name.startswith("."):
            # ``__pycache__`` and similar housekeeping dirs.
            continue
        manifest_path = child / _MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        variants.append(_load_variant(child, manifest_path))
    return variants


def _load_variant(root: Path, manifest_path: Path) -> Variant:
    """Parse one manifest and wrap it in a :class:`Variant`."""
    try:
        with manifest_path.open("rb") as fh:
            raw = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ExamplesRegistryError(f"failed to read variant manifest at {manifest_path}: {exc}") from exc
    try:
        manifest = VariantManifest.model_validate(raw)
    except Exception as exc:
        raise ExamplesRegistryError(f"manifest at {manifest_path} is invalid: {exc}") from exc
    return Variant(id=root.name, root=root, manifest=manifest)


__all__ = [
    "ExamplesRegistryError",
    "Variant",
    "VariantManifest",
    "allowed_starter_templates",
    "get_variant",
    "list_starter_options",
    "list_variants",
]
