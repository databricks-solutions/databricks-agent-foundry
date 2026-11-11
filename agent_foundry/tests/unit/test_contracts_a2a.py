"""Unit tests for ``agent_foundry.contracts.a2a``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_foundry.contracts.a2a import (
    A2AAgentCapabilities,
    A2AAgentCardConfig,
    A2AAgentProvider,
    A2AAgentSkill,
    A2AConfig,
)


def _skill(skill_id: str = "ask", name: str = "Ask") -> dict:
    return {"id": skill_id, "name": name, "description": "d"}


def _minimal_card(**overrides) -> dict:
    base = {
        "name": "Test Agent",
        "description": "test",
        "version": "1.0.0",
        "skills": [_skill()],
    }
    base.update(overrides)
    return base


class TestA2AAgentSkill:
    def test_required_fields_only(self) -> None:
        s = A2AAgentSkill(id="ask", name="Ask", description="d")
        assert s.tags == []
        assert s.examples == []
        # Per-skill modes default to None so the builder can fall back
        # to the card-level defaults.
        assert s.input_modes is None
        assert s.output_modes is None

    def test_full(self) -> None:
        s = A2AAgentSkill(
            id="ask",
            name="Ask",
            description="d",
            tags=["finance"],
            examples=["q?"],
            input_modes=["text"],
            output_modes=["text", "audio"],
        )
        assert s.tags == ["finance"]
        assert s.input_modes == ["text"]
        assert s.output_modes == ["text", "audio"]

    def test_missing_id_rejected(self) -> None:
        with pytest.raises(ValidationError):
            A2AAgentSkill.model_validate({"name": "n", "description": "d"})


class TestA2AAgentCapabilities:
    def test_defaults_match_streaming_a2a(self) -> None:
        c = A2AAgentCapabilities()
        assert c.streaming is True
        assert c.push_notifications is False

    def test_overrides(self) -> None:
        c = A2AAgentCapabilities(streaming=False, push_notifications=True)
        assert c.streaming is False
        assert c.push_notifications is True


class TestA2AAgentProvider:
    def test_both_optional(self) -> None:
        p = A2AAgentProvider()
        assert p.organization is None
        assert p.url is None

    def test_partial(self) -> None:
        p = A2AAgentProvider(organization="Acme")
        assert p.organization == "Acme"
        assert p.url is None


class TestA2AAgentCardConfig:
    def test_minimal(self) -> None:
        c = A2AAgentCardConfig.model_validate(_minimal_card())
        assert c.name == "Test Agent"
        assert c.version == "1.0.0"
        assert c.capabilities.streaming is True
        assert c.default_input_modes == ["text"]
        assert c.default_output_modes == ["text"]
        assert c.provider is None

    def test_full(self) -> None:
        c = A2AAgentCardConfig.model_validate(
            {
                "name": "X",
                "description": "d",
                "version": "1.2.3",
                "documentation_url": "https://docs",
                "icon_url": "https://icon",
                "provider": {"organization": "Acme", "url": "https://acme"},
                "capabilities": {"streaming": False, "push_notifications": True},
                "default_input_modes": ["text"],
                "default_output_modes": ["text", "audio"],
                "skills": [
                    _skill("a"),
                    {
                        "id": "b",
                        "name": "B",
                        "description": "d",
                        "tags": ["t"],
                        "examples": ["e"],
                        "input_modes": ["audio"],
                    },
                ],
            }
        )
        assert c.provider is not None
        assert c.provider.organization == "Acme"
        assert c.capabilities.streaming is False
        assert len(c.skills) == 2
        assert c.skills[1].input_modes == ["audio"]
        # Per-skill output_modes stays None when omitted -- the builder
        # is responsible for substituting card-level defaults.
        assert c.skills[1].output_modes is None

    def test_missing_required_field_rejected(self) -> None:
        with pytest.raises(ValidationError):
            A2AAgentCardConfig.model_validate({"description": "d", "version": "1.0.0", "skills": [_skill()]})

    @pytest.mark.parametrize(
        "bad",
        ["", "1", "1.0", "v1.0.0", "1.0.0.0", "1.a.0", "latest"],
    )
    def test_invalid_version_rejected(self, bad: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            A2AAgentCardConfig.model_validate(_minimal_card(version=bad))
        assert "semver" in str(exc_info.value).lower()

    @pytest.mark.parametrize(
        "good",
        ["1.0.0", "0.0.1", "10.20.30", "1.0.0-rc.1", "1.0.0+build.7"],
    )
    def test_valid_version_accepted(self, good: str) -> None:
        c = A2AAgentCardConfig.model_validate(_minimal_card(version=good))
        assert c.version == good

    def test_empty_skills_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            A2AAgentCardConfig.model_validate(_minimal_card(skills=[]))
        assert "non-empty" in str(exc_info.value)

    def test_duplicate_skill_id_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            A2AAgentCardConfig.model_validate(_minimal_card(skills=[_skill("dup"), _skill("dup", "Other")]))
        msg = str(exc_info.value)
        assert "duplicate skill id" in msg
        assert "dup" in msg

    def test_extra_keys_ignored(self) -> None:
        c = A2AAgentCardConfig.model_validate(_minimal_card(unknown_future_field="x"))
        assert not hasattr(c, "unknown_future_field")


class TestA2AConfig:
    def test_empty(self) -> None:
        cfg = A2AConfig()
        assert cfg.card is None

    def test_card_set(self) -> None:
        cfg = A2AConfig.model_validate({"card": _minimal_card()})
        assert cfg.card is not None
        assert cfg.card.name == "Test Agent"

    def test_unwrapped_card_fields_rejected(self) -> None:
        """A common mistake is to put card fields directly under
        ``a2a:`` instead of under ``a2a.card:``.  Without the explicit
        check ``extra='ignore'`` would silently drop them and ship an
        empty card."""
        with pytest.raises(ValidationError) as exc_info:
            A2AConfig.model_validate(_minimal_card())
        msg = str(exc_info.value)
        assert "wrap the card under 'card:'" in msg
        assert "name" in msg

    def test_extra_top_level_keys_ignored(self) -> None:
        """Future a2a-only options (e.g. timeouts) must not break older
        framework versions that have not learned about them yet."""
        cfg = A2AConfig.model_validate({"card": _minimal_card(), "future_option": True})
        assert cfg.card is not None
        assert not hasattr(cfg, "future_option")


class TestAgentPayloadSpecIntegration:
    """Cross-module check: the A2A card is reachable through the agent
    payload spec (``spec.integrations.a2a``)."""

    def test_a2a_card_default_none(self) -> None:
        from payloads.agent.contracts import AgentPayloadSpec

        spec = AgentPayloadSpec()
        assert spec.integrations.a2a.card is None

    def test_a2a_block_parsed_through_payload_spec(self) -> None:
        from payloads.agent.contracts import AgentPayloadSpec

        spec = AgentPayloadSpec.model_validate(
            {
                "runtime": {"model": {"kind": "serving_endpoint", "name": "ep"}},
                "integrations": {"a2a": {"card": _minimal_card()}},
            }
        )
        assert spec.integrations.a2a.card is not None
        assert spec.integrations.a2a.card.name == "Test Agent"


class TestGetA2AConfigAccessor:
    def test_returns_none_when_a2a_absent(self) -> None:
        from payloads.agent.config import AgentConfig, get_a2a_config

        cfg = AgentConfig()
        assert get_a2a_config(cfg) is None

    def test_returns_a2a_when_present(self) -> None:
        from payloads.agent.config import (
            AgentConfig,
            agent_config_kwargs_from_spec,
            get_a2a_config,
        )
        from payloads.agent.contracts import AgentPayloadSpec

        spec = AgentPayloadSpec.model_validate({"integrations": {"a2a": {"card": _minimal_card()}}})
        cfg = AgentConfig(**agent_config_kwargs_from_spec(spec))
        out = get_a2a_config(cfg)
        assert out is not None
        assert out.card is not None
        assert out.card.name == "Test Agent"
