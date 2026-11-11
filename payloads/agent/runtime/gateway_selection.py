"""``custom_inputs.selected_resources`` -- the user's picks from the resources sidebar.

The chat UI attaches the saved selection server-side, but the agent treats
it as untrusted input: this module only checks the *shape*, and every
entry is re-checked against the static catalog policy in
:mod:`payloads.agent.runtime.gateway_resolve` before anything is mounted.
A malformed or oversized value is rejected whole.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

logger = logging.getLogger(__name__)

SELECTED_RESOURCES_INPUT = "selected_resources"
MAX_SELECTIONS = 64
_MAX_FQN_CHARS = 255

ResourceKind = Literal["mcp_service", "skill"]


class SelectedResource(BaseModel):
    """One sidebar pick: a resource kind and its Unity Catalog name."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ResourceKind
    fqn: str = Field(min_length=1, max_length=_MAX_FQN_CHARS)


_SELECTIONS: TypeAdapter[list[SelectedResource]] = TypeAdapter(
    Annotated[list[SelectedResource], Field(max_length=MAX_SELECTIONS)]
)


@dataclass(frozen=True)
class Selections:
    """Parsed selections; ``rejected`` says why the whole input was refused."""

    resources: tuple[SelectedResource, ...] = ()
    rejected: str | None = None

    def of_kind(self, kind: ResourceKind) -> list[SelectedResource]:
        return [resource for resource in self.resources if resource.kind == kind]


def parse_selections(custom_inputs: Mapping[str, Any] | None) -> Selections:
    """Read ``selected_resources``; missing means baseline only."""
    raw = (custom_inputs or {}).get(SELECTED_RESOURCES_INPUT)
    if raw is None:
        return Selections()
    try:
        resources = _SELECTIONS.validate_python(raw)
    except ValidationError as exc:
        reason = f"invalid custom_inputs.{SELECTED_RESOURCES_INPUT} ({exc.error_count()} error(s))"
        logger.warning("[gateway] ignoring selections: %s", reason)
        return Selections(rejected=reason)
    return Selections(resources=tuple(resources))
