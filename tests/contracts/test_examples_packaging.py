"""CI guards for ``examples/`` variant packaging and discovery."""

from __future__ import annotations

from pathlib import Path

from agent_foundry.init.examples_registry import list_variants
from agent_foundry.packaging.examples_variants import (
    EXAMPLES_PACKAGE_DATA_GLOBS,
    discover_variant_dir_ids,
    per_variant_package_data_keys,
    read_examples_package_data_globs,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLES_DIR = _REPO_ROOT / "examples"


def test_examples_package_data_uses_parent_globs_only() -> None:
    """Wheel packaging must use one ``examples`` entry, not per-variant lines."""
    globs = read_examples_package_data_globs()
    assert sorted(globs) == sorted(EXAMPLES_PACKAGE_DATA_GLOBS)
    legacy = per_variant_package_data_keys()
    assert legacy == [], (
        "remove per-variant [tool.setuptools.package-data] entries; "
        f"parent 'examples' globs cover all variants. Found: {legacy}"
    )


def test_every_variant_dir_is_discovered_by_registry() -> None:
    on_disk = discover_variant_dir_ids(examples_dir=_EXAMPLES_DIR)
    registered = [v.id for v in list_variants()]
    assert on_disk == registered, (
        "examples/ on-disk variants must match examples_registry.list_variants(); "
        f"on_disk={on_disk!r} registered={registered!r}"
    )


def test_variant_dirs_require_init_and_manifest() -> None:
    """Authoring contract for new variants (see examples/README.md)."""
    for variant_id in discover_variant_dir_ids(examples_dir=_EXAMPLES_DIR):
        root = _EXAMPLES_DIR / variant_id
        assert (root / "__init__.py").is_file(), f"{variant_id} missing __init__.py"
        assert (root / "manifest.toml").is_file(), f"{variant_id} missing manifest.toml"
        assert (root / "foundry.toml").is_file(), f"{variant_id} missing foundry.toml"
