"""Tests for the central env-var registry (issue #155)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_foundry import env

# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_has_unique_names() -> None:
    names = [v.name for v in env.REGISTRY]
    assert len(names) == len(set(names)), "duplicate env-var names in REGISTRY"


def test_registry_module_constants_match_registry() -> None:
    """Every EnvVar exported as a module constant is also in REGISTRY,
    and vice versa.  Catches the common bug of declaring a new var but
    forgetting to add it to the tuple (or removing it from the tuple
    but leaving the constant)."""
    constants = {getattr(env, name) for name in env.__all__ if isinstance(getattr(env, name), env.EnvVar)}
    assert constants == set(env.REGISTRY)


# ---------------------------------------------------------------------------
# read / read_optional / read_int / read_bool
# ---------------------------------------------------------------------------


def test_read_returns_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env.DEPLOY_TARGET.name, "staging")
    assert env.read(env.DEPLOY_TARGET) == "staging"


def test_read_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(env.DEPLOY_TARGET.name, raising=False)
    assert env.read(env.DEPLOY_TARGET) == "dev"


def test_read_raises_when_no_value_no_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(env.AGENT_FOUNDRY_PROJECT_ROOT.name, raising=False)
    with pytest.raises(env.MissingEnvVar):
        env.read(env.AGENT_FOUNDRY_PROJECT_ROOT)


def test_read_optional_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(env.DATABRICKS_CONFIG_PROFILE.name, raising=False)
    assert env.read_optional(env.DATABRICKS_CONFIG_PROFILE) is None


def test_read_int_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(env.CHAT_APP_PORT.name, "8080")
    assert env.read_int(env.CHAT_APP_PORT) == 8080


def test_read_int_uses_fallback_on_parse_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(env.CHAT_APP_PORT.name, "not-a-number")
    assert env.read_int(env.CHAT_APP_PORT, fallback=3000) == 3000


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("true", True),
        ("True", True),
        ("1", True),
        ("yes", True),
        ("YES", True),
        ("false", False),
        ("0", False),
        ("no", False),
        ("", False),  # empty string -> default fallback
    ],
)
def test_read_bool(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv(env.ENABLE_MCP_SERVER.name, raw)
    assert env.read_bool(env.ENABLE_MCP_SERVER) is expected


# ---------------------------------------------------------------------------
# Per-context validation
# ---------------------------------------------------------------------------


def test_validate_for_runtime_passes_with_required_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in env.vars_for("runtime"):
        monkeypatch.setenv(var.name, "x")
    env.validate_for("runtime")


def test_validate_for_runtime_raises_with_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in env.vars_for("runtime"):
        monkeypatch.delenv(var.name, raising=False)
    with pytest.raises(env.MissingEnvVar) as excinfo:
        env.validate_for("runtime")
    msg = str(excinfo.value)
    for var in env.vars_for("runtime"):
        if var.default is None:
            assert var.name in msg


# ---------------------------------------------------------------------------
# Docs sync
# ---------------------------------------------------------------------------


def test_env_docs_in_sync() -> None:
    """``docs/reference/env-vars.md`` must exactly match what
    :func:`env.render_docs` produces.  Regenerate via
    ``python -m agent_foundry.env --generate-docs``.
    """
    repo_root = Path(__file__).resolve().parents[3]
    docs_path = repo_root / "docs" / "reference" / "env-vars.md"
    assert docs_path.exists(), f"missing {docs_path}"
    on_disk = docs_path.read_text(encoding="utf-8")
    assert on_disk == env.render_docs(), (
        "docs/reference/env-vars.md is out of sync with agent_foundry.env.REGISTRY. "
        "Run: python -m agent_foundry.env --generate-docs"
    )
