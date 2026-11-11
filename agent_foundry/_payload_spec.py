"""Read the agent payload's ``spec`` from ``payload.yml``.

``payloads/<payload>/payload.yml`` is the single per-payload config
contract.  Its ``payload.spec`` block is the one source for the payload's
runtime config, UI feature toggles, and external integrations.  This
module is the shared low-level reader so the loader, the skills
assembler, and the deploy glue all see the same mapping.

Reads are pure (no validation, no ``${var.*}`` substitution): structural
validation lives with :class:`agent_foundry.contracts.agent.AgentPayloadSpec`
(deploy/runtime).  This is a hard-cut surface -- there is no legacy
``capabilities`` / ``config.yml`` fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from agent_foundry._instance_layout import DEFAULT_PAYLOAD, payload_root


def read_spec(instance_dir: Path, *, payload: str = DEFAULT_PAYLOAD) -> dict[str, Any]:
    """Return the payload's ``spec`` mapping (``{}`` when absent).

    Reads ``payloads/<payload>/payload.yml`` and returns ``payload.spec``.
    Malformed shapes degrade to ``{}`` so callers can apply their own
    defaults; the typed contract is the source of truth for rejecting
    bad input at deploy/runtime.
    """
    path = payload_root(instance_dir, payload) / "payload.yml"
    if not path.exists():
        return {}

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    payload_block = raw.get("payload") if isinstance(raw, dict) else None
    spec = payload_block.get("spec") if isinstance(payload_block, dict) else None
    return spec if isinstance(spec, dict) else {}


def spec_section(spec: dict[str, Any] | None, *keys: str) -> dict[str, Any]:
    """Return a nested mapping under *spec* by *keys* (``{}`` when missing).

    Example: ``spec_section(spec, "runtime", "tools", "skills")`` returns
    ``spec["runtime"]["tools"]["skills"]`` when every level is a mapping,
    else ``{}``.
    """
    current: Any = spec or {}
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


__all__ = ["read_spec", "spec_section"]
