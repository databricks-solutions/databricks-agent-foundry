"""Per-instance payload config-surface layout.

A payload owns its full authored surface under
``instances/<name>/payloads/<payload>/``:

    instances/<name>/
      payloads/<payload>/
        payload.yml      # adapter id + spec (the single config contract)
        skills/          # (agent payload) skill providers
        mcp_servers/     # (agent payload) MCP tool providers

This mirrors the framework's own ``payloads/<payload>/`` code layout.
``app/`` stays a generic host shell (compute, OBO scopes, app resources,
payload selection); everything payload-specific lives under
``payloads/<payload>/``.

The helpers here are payload-generic -- they take a ``payload`` name (so
future non-agent payloads use the same layout) and resolve only the
canonical location.  There is no legacy fallback: this is the 2.0
contract.
"""

from __future__ import annotations

from pathlib import Path

PAYLOADS_DIRNAME = "payloads"
DEFAULT_PAYLOAD = "agent"


def payload_root(instance_dir: Path, payload: str = DEFAULT_PAYLOAD) -> Path:
    """Canonical per-payload config dir for *instance_dir*.

    ``instances/<name>/payloads/<payload>/`` -- the home for that
    payload's ``payload.yml`` and any provider subdirs.
    """
    return instance_dir / PAYLOADS_DIRNAME / payload


def resolve_payload_subdir(
    instance_dir: Path,
    subdir: str,
    *,
    payload: str = DEFAULT_PAYLOAD,
) -> Path:
    """Resolve a payload-owned subdir (e.g. ``skills`` / ``mcp_servers``).

    Returns ``payloads/<payload>/<subdir>`` -- the canonical location,
    whether or not it exists yet.
    """
    return payload_root(instance_dir, payload) / subdir


def has_payload(instance_dir: Path, payload: str = DEFAULT_PAYLOAD) -> bool:
    """True when *instance_dir* carries the named payload's config surface.

    Used by payload adapters' ``applies_to`` to claim an instance.
    """
    return payload_root(instance_dir, payload).is_dir()


def discover_payloads(instance_dir: Path) -> list[str]:
    """Return the payload names that carry a ``payload.yml`` manifest.

    Scans ``instances/<name>/payloads/*/payload.yml`` so the App host can
    select a non-``agent`` payload (e.g. ``rest_mcp``) without the framework
    hard-coding payload names.  Sorted for deterministic behaviour; an
    instance is expected to carry exactly one payload manifest.
    """
    payloads_dir = instance_dir / PAYLOADS_DIRNAME
    if not payloads_dir.is_dir():
        return []
    return sorted(manifest.parent.name for manifest in payloads_dir.glob("*/payload.yml"))


__all__ = [
    "DEFAULT_PAYLOAD",
    "PAYLOADS_DIRNAME",
    "discover_payloads",
    "has_payload",
    "payload_root",
    "resolve_payload_subdir",
]
