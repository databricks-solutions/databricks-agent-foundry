"""Typed contracts for the A2A (Agent-to-Agent) public agent card.

These models describe what an agent advertises to **external A2A
consumers** via the agent card served at
``/api/.well-known/agent-card.json``.  They are deliberately separate
from :class:`agent_foundry.contracts.skills.SkillMetadata`, which
describes capabilities the agent **hosts internally** -- the two
surfaces have different audiences (external clients vs. the agent's
own runtime) and different lifecycles, and should not be coupled.

Field shapes mirror the ``a2a-sdk`` protobuf types (see
``a2a/types/a2a_pb2.py`` in the installed wheel) but use the
framework's snake_case + Pydantic conventions so an instance owner can
author the card in ``payloads/agent/payload.yml`` (``spec.integrations.a2a``)
without touching the SDK directly.  The payload-side builder in
``payloads/agent/runtime/a2a_adapter.py`` maps these models to the proto
types at registration time.

Authoring shape (in ``payloads/agent/payload.yml`` ``spec.integrations.a2a``)::

    a2a:
      card:
        name: "Portfolio Assistant"
        description: "Databricks AI agent for portfolio analytics."
        version: "1.0.0"
        provider:
          organization: "Databricks Field Eng"
        capabilities:
          streaming: true
        default_input_modes: ["text"]
        default_output_modes: ["text"]
        skills:
          - id: "ask_portfolio"
            name: "Ask about portfolio"
            description: "Answer questions about portfolio holdings."
            tags: ["finance"]
            examples: ["What is the revenue for Q4?"]

When the ``a2a:`` block is absent the brick falls back to a hardcoded
default card so existing instances keep working unchanged.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

_MODEL_CONFIG = ConfigDict(extra="ignore")

# Loose semver: ``MAJOR.MINOR.PATCH`` with an optional pre-release /
# build suffix.  Strict semver (https://semver.org) is over-prescriptive
# for hand-authored card versions; we just want to catch typos like
# ``v1`` or ``1.0`` early.
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+]\S+)?$")


class A2AAgentProvider(BaseModel):
    """Optional organisation that publishes the agent.

    Emitted on the wire only when at least one field is set.
    """

    model_config = _MODEL_CONFIG

    organization: str | None = None
    url: str | None = None


class A2AAgentCapabilities(BaseModel):
    """A2A capability flags advertised on the card."""

    model_config = _MODEL_CONFIG

    streaming: bool = True
    push_notifications: bool = False


class A2AAgentSkill(BaseModel):
    """One external-facing skill in the agent card.

    ``input_modes`` / ``output_modes`` default to ``None`` so the builder
    can substitute the card-level defaults (``default_input_modes`` /
    ``default_output_modes``) per skill -- keeping the authored YAML
    minimal when every skill uses the same modes.
    """

    model_config = _MODEL_CONFIG

    id: str
    name: str
    description: str
    tags: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    input_modes: list[str] | None = None
    output_modes: list[str] | None = None


class A2AAgentCardConfig(BaseModel):
    """Full agent card configuration.

    Validation rules:
      - ``version`` must match a loose semver shape (``MAJOR.MINOR.PATCH``
        with an optional pre-release / build suffix).
      - ``skills`` must be non-empty -- a card without skills tells
        external consumers nothing.
      - Skill ``id`` values must be unique within the card -- duplicate
        ids would silently collide on the wire and confuse clients.
    """

    model_config = _MODEL_CONFIG

    name: str
    description: str
    version: str
    documentation_url: str | None = None
    icon_url: str | None = None
    provider: A2AAgentProvider | None = None
    capabilities: A2AAgentCapabilities = Field(default_factory=A2AAgentCapabilities)
    default_input_modes: list[str] = Field(default_factory=lambda: ["text"])
    default_output_modes: list[str] = Field(default_factory=lambda: ["text"])
    skills: list[A2AAgentSkill]

    @model_validator(mode="after")
    def _validate_card(self) -> A2AAgentCardConfig:
        if not _VERSION_RE.match(self.version):
            raise ValueError(
                f"a2a.card.version {self.version!r} is not a valid semver "
                "(expected MAJOR.MINOR.PATCH, with an optional -prerelease "
                "or +build suffix; e.g. '1.0.0', '1.2.0-rc.1')"
            )
        if not self.skills:
            raise ValueError(
                "a2a.card.skills must be non-empty: a card without skills advertises nothing to external A2A consumers"
            )
        seen: set[str] = set()
        for skill in self.skills:
            if skill.id in seen:
                raise ValueError(
                    f"a2a.card.skills contains a duplicate skill id {skill.id!r}; ids must be unique within the card"
                )
            seen.add(skill.id)
        return self


class A2AConfig(BaseModel):
    """Wrapper for the ``a2a`` block (``spec.integrations.a2a`` in ``payloads/agent/payload.yml``).

    Wrapping the card in a top-level ``a2a:`` block (rather than
    putting ``agent_card:`` at the root) leaves room for future
    a2a-only runtime options -- e.g. task store choice, per-route
    timeouts, auth scheme references -- without breaking the schema
    or splitting the configuration surface.
    """

    model_config = _MODEL_CONFIG

    card: A2AAgentCardConfig | None = None
    enable_v0_3_compat: bool = Field(
        default=False,
        description=(
            "Advertise a v0.3 AgentInterface alongside the v1.0 one and "
            "enable v0.3->v1.0 JSON-RPC payload translation in the SDK. "
            "Opt-in; required for clients still on the pre-1.0 A2A spec. "
            "When false (default) only the v1.0 interface is advertised "
            "and the SDK's compat translation layer is disabled."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _reject_unwrapped_card_fields(cls, value: Any) -> Any:
        """Catch the common mistake of putting card fields at the
        ``a2a:`` root instead of under ``a2a.card:``.

        Without this hint, ``extra='ignore'`` would silently drop the
        misplaced fields and ship an empty card.  We only flag the
        unambiguous case where a required ``A2AAgentCardConfig`` field
        appears at the wrong level.
        """
        if not isinstance(value, dict):
            return value
        misplaced = {"name", "description", "version", "skills"} & set(value)
        if misplaced and "card" not in value:
            raise ValueError(
                f"a2a: block must wrap the card under 'card:' (found card field(s) {sorted(misplaced)} at the a2a root)"
            )
        return value


__all__ = [
    "A2AAgentCapabilities",
    "A2AAgentCardConfig",
    "A2AAgentProvider",
    "A2AAgentSkill",
    "A2AConfig",
]
