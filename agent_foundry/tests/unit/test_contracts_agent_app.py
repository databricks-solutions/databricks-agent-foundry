"""Unit tests for ``agent_foundry.contracts.agent_app``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_foundry.contracts.agent_app import AgentAppConfig


class TestAgentAppConfig:
    def test_defaults(self) -> None:
        """A bare config defaults to medium with empty resource lists."""
        c = AgentAppConfig()
        assert c.compute_size == "medium"
        assert c.app_resources == []
        assert c.sp_grants == []
        assert c.user_authorization == []

    def test_accepts_large(self) -> None:
        c = AgentAppConfig(compute_size="large")
        assert c.compute_size == "large"

    def test_rejects_bad_value(self) -> None:
        """An out-of-enum compute_size fails via the Literal type."""
        with pytest.raises(ValidationError, match="compute_size"):
            AgentAppConfig.model_validate({"compute_size": "small"})

    def test_rejects_typoed_key(self) -> None:
        """extra='forbid' catches a misspelled key so a deploy fails
        fast instead of silently using the default size."""
        with pytest.raises(ValidationError, match="compute_sizee"):
            AgentAppConfig.model_validate({"compute_sizee": "large"})

    def test_accepts_known_scopes(self) -> None:
        c = AgentAppConfig(user_authorization=["sql", "mcp.genie", "postgres"])
        assert "postgres" in c.user_authorization

    def test_rejects_unknown_scope(self) -> None:
        """An OBO scope outside the closed enum fails fast."""
        with pytest.raises(ValidationError, match="user_authorization"):
            AgentAppConfig.model_validate({"user_authorization": ["sql", "not-a-scope"]})

    def test_passthrough_blocks_preserved(self) -> None:
        """The loosely-typed resource blocks round-trip unchanged."""
        raw = {
            "compute_size": "large",
            "app_resources": [{"name": "x", "serving_endpoint": {"name": "ep"}}],
            "sp_grants": [{"securable_type": "CATALOG", "securable_name": "c"}],
            "user_authorization": ["sql", "dashboards.genie"],
        }
        dumped = AgentAppConfig.model_validate(raw).model_dump()
        assert dumped["app_resources"] == raw["app_resources"]
        assert dumped["sp_grants"] == raw["sp_grants"]
        assert dumped["user_authorization"] == raw["user_authorization"]
