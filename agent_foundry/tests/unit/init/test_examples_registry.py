"""Tests for :mod:`agent_foundry.init.examples_registry`."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry.init.examples_registry import (
    ExamplesRegistryError,
    VariantManifest,
    allowed_starter_templates,
    get_variant,
    list_starter_options,
    list_variants,
)


def test_list_starter_options_includes_blank_and_variants() -> None:
    options = list_starter_options()
    values = [value for value, _ in options]
    assert values[0] == "blank"
    assert "portfolio_assistant" in values
    assert any("Portfolio Assistant" in help_text for _, help_text in options)


def test_allowed_starter_templates_matches_options() -> None:
    assert allowed_starter_templates() == [v for v, _ in list_starter_options()]


def test_list_variants_includes_portfolio_assistant() -> None:
    variants = list_variants()
    ids = [v.id for v in variants]
    assert "portfolio_assistant" in ids


def test_portfolio_assistant_manifest_parses() -> None:
    variant = get_variant("portfolio_assistant")
    assert variant.manifest == VariantManifest(
        display_name="Portfolio Assistant",
        description="End-to-end production agent: data, retrieval, evaluation, monitoring, operations",
    )


def test_portfolio_assistant_root_has_foundry_toml() -> None:
    variant = get_variant("portfolio_assistant")
    assert (variant.root / "foundry.toml").is_file()
    assert (variant.root / "manifest.toml").is_file()


def test_get_variant_unknown_raises() -> None:
    with pytest.raises(ExamplesRegistryError, match="unknown variant"):
        get_variant("nonexistent-variant-xyz")


def test_variant_manifest_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(Exception):
        VariantManifest.model_validate(
            {
                "display_name": "X",
                "description": "Y",
                "extra_field": "nope",
            }
        )
