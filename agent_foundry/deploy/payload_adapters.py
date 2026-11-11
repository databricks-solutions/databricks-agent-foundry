"""App payload adapters (issue #338).

The generic Databricks App host (:mod:`agent_foundry.deploy.app_host`)
owns App *infrastructure*; a **payload adapter** owns the runtime code
that runs inside the container and knows how to translate the payload's
own config into the neutral :class:`~agent_foundry.deploy.app_host.AppPayloadSpec`
the host consumes.

This keeps the framework stable as payloads evolve: the App host only
checks that the named adapter exists and returns a valid spec -- it never
learns a payload's internals (skills, MCP routes, A2A card handling, UI
process management, file upload, ...).

Adapters are looked up by the ``payload.adapter`` name in
``app/payload.yml``.  The built-in ``agent`` adapter lives in the
``agent`` payload package (``payloads/agent/``) and is imported lazily
(mirroring :func:`agent_foundry.bricks._builtin_bricks`) so this
framework module does not import payload code at import time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from importlib import metadata as _metadata
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import yaml
from pydantic import ValidationError

from agent_foundry._instance_layout import DEFAULT_PAYLOAD, discover_payloads, payload_root
from agent_foundry.contracts.payload import PayloadConfig, PayloadFile
from agent_foundry.loader import FoundryConfigError

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from agent_foundry.bricks import (
        BrickResult,
        DeployContext,
        DestroyContext,
        DestroyPlanItem,
    )
    from agent_foundry.deploy.app_host import AppInfraSpec, AppPayloadSpec
    from agent_foundry.scaffold_spec import ScaffoldDecl

# Entry-point group third-party distributions declare under to register a
# custom payload adapter (mirrors ``agent_foundry.bricks``'s
# :data:`~agent_foundry.bricks.ENTRY_POINT_GROUP`).  Example (downstream
# ``pyproject.toml``)::
#
#     [project.entry-points."agent_foundry.payload_adapters"]
#     rest_mcp = "my_pkg.adapter:RestMcpPayloadAdapter"
ENTRY_POINT_GROUP = "agent_foundry.payload_adapters"


@runtime_checkable
class AppPayloadAdapter(Protocol):
    """A Databricks App payload's deploy-time contract (issue #338).

    The generic App host (:mod:`agent_foundry.deploy.app_host`) drives a
    fixed deploy sequence and depends only on this structural type, so it
    never learns a payload's internals (skills, MCP routes, A2A card
    handling, UI process management, file upload, Lakebase, ...).  A
    payload supplies, via this protocol:

    * ``name`` / ``applies_to`` -- registry identity and selection;
    * ``build_payload_spec`` -- the runtime contract (command/env/extras)
      that runs *inside* the container;
    * ``build_infra`` -- the neutral infrastructure the host provisions
      *around* the container (compute/resources/scopes/grants);
    * ``after_reconcile`` -- an optional heal hook between spec reconcile
      and code deploy (no-op for payloads that need none).
    """

    name: str

    def applies_to(self, instance_dir: Path, config: Mapping[str, object]) -> bool:
        """Whether this adapter can serve the payload for *instance_dir*."""
        ...

    def build_payload_spec(
        self,
        instance_dir: Path | None,
        variables: Mapping[str, str],
        spec: Mapping[str, object],
    ) -> AppPayloadSpec:
        """Resolve the payload's runtime contract into an ``AppPayloadSpec``.

        ``spec`` is the opaque ``payload.spec`` mapping from the
        payload manifest; the framework passes it through verbatim and the
        adapter interprets it (the framework never learns its shape).
        """
        ...

    def build_infra(
        self,
        instance_dir: Path | None,
        variables: Mapping[str, str],
        *,
        target: str,
    ) -> AppInfraSpec:
        """Translate the payload's config into the neutral ``AppInfraSpec``."""
        ...

    def after_reconcile(
        self,
        ws: WorkspaceClient | None,
        app_name: str,
        variables: Mapping[str, str],
        *,
        dry_run: bool,
    ) -> None:
        """Optional post-reconcile, pre-deploy heal hook (default no-op)."""
        ...

    def provision(self, ctx: DeployContext) -> int:
        """Deploy-time pre-step run before the App deploy (e.g. Lakebase).

        May mutate ``ctx.variables`` in place with discovered outputs.
        Returns 0 on success (or when there is nothing to provision); a
        non-zero rc aborts the App deploy.
        """
        ...

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]:
        """Payload-owned teardown items (in addition to the App itself)."""
        ...

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        """Tear down payload-owned resources (in addition to the App)."""
        ...

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        """Payload-owned deploy variables to republish on (re)deploy.

        Lets the App host merge payload-owned state (e.g. cached resource
        paths from a previous provision) back into ``ctx.variables``
        without the host learning the payload's internals.  Returns an
        empty dict when there is nothing to publish (first deploy, or the
        owning capability was never enabled).
        """
        ...

    def scaffolds(self) -> Sequence[ScaffoldDecl]:
        """Scaffold verbs this payload contributes to ``agent-foundry scaffold``.

        Each :class:`~agent_foundry.scaffold_spec.ScaffoldDecl` names a
        CLI verb and the copy operations that materialise it.  The
        framework composes these with its own brick scaffolds, so a
        payload adds authoring surfaces (its manifest, tool providers,
        ...) without any framework edit.  Defaults to none.
        """
        ...


def load_payload_config(instance_dir: Path | None) -> PayloadConfig:
    """Read the generic payload manifest (``payloads/<payload>/payload.yml``).

    Payload-agnostic: discovers the instance's single payload manifest
    (``payloads/*/payload.yml``) and returns the neutral
    :class:`PayloadConfig` (``adapter`` name + opaque ``spec``) that selects
    which :class:`AppPayloadAdapter` the App host drives.  Defaults to the
    framework :data:`~agent_foundry._instance_layout.DEFAULT_PAYLOAD`
    adapter when no manifest is present so freshly-scaffolded instances
    still deploy.  Raises when more than one manifest is present -- an
    instance's App hosts exactly one payload.
    """
    default = PayloadConfig(adapter=DEFAULT_PAYLOAD, requires=["tracing", "observability"])
    if instance_dir is None:
        return default

    payload_names = discover_payloads(instance_dir)
    if not payload_names:
        return default
    if len(payload_names) > 1:
        raise FoundryConfigError(
            f"Instance {instance_dir.name!r} carries multiple payload manifests {payload_names}; "
            "an App hosts exactly one payload."
        )

    payload_path = payload_root(instance_dir, payload_names[0]) / "payload.yml"
    raw = yaml.safe_load(payload_path.read_text(encoding="utf-8")) or {}
    try:
        return PayloadFile.model_validate(raw).payload
    except ValidationError as exc:
        raise FoundryConfigError(f"Invalid payload.yml: {exc}") from exc


def _builtin_adapters() -> tuple[AppPayloadAdapter, ...]:
    """Lazily import and instantiate the built-in payload adapters.

    Imported inside the function (not at module top) so this framework
    module stays free of payload imports at load time, matching the
    layering used by :func:`agent_foundry.bricks._builtin_bricks`.
    """
    from payloads.agent.adapter import AgentPayloadAdapter
    from payloads.rest_mcp.adapter import RestMcpPayloadAdapter

    return (AgentPayloadAdapter(), RestMcpPayloadAdapter())


def _third_party_adapters() -> list[AppPayloadAdapter]:
    """Load adapters registered via the ``agent_foundry.payload_adapters``
    entry-point group.

    Mirrors :func:`agent_foundry.bricks._third_party_bricks`: distributions
    that register nothing are silently ignored; a load failure surfaces as
    a ``RuntimeError`` (a broken third-party adapter is worse than a
    missing one).  Entry points may expose the class or an instance.
    """
    adapters: list[AppPayloadAdapter] = []
    for ep in _metadata.entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to load {ENTRY_POINT_GROUP} entry point {ep.name!r} = {ep.value!r}: {exc}"
            ) from exc
        adapter = obj() if isinstance(obj, type) else obj
        if not isinstance(adapter, AppPayloadAdapter):
            raise RuntimeError(
                f"{ENTRY_POINT_GROUP} entry point {ep.name!r} = {ep.value!r} "
                f"does not implement the AppPayloadAdapter protocol."
            )
        adapters.append(adapter)
    return adapters


def iter_payload_adapters() -> list[AppPayloadAdapter]:
    """Return all registered payload adapters: built-ins then third-party.

    When a third-party adapter declares the same ``name`` as a built-in,
    the third-party instance wins -- the intended override path, matching
    :func:`agent_foundry.bricks.iter_bricks`.
    """
    by_name: dict[str, AppPayloadAdapter] = {a.name: a for a in _builtin_adapters()}
    for adapter in _third_party_adapters():
        by_name[adapter.name] = adapter
    return list(by_name.values())


def get_payload_adapter(name: str) -> AppPayloadAdapter:
    """Return the payload adapter registered under *name*.

    Raises
    ------
    FoundryConfigError
        With the available names when *name* is unknown.
    """
    adapters = {adapter.name: adapter for adapter in iter_payload_adapters()}
    adapter = adapters.get(name)
    if adapter is None:
        choices = ", ".join(sorted(adapters))
        raise FoundryConfigError(f"Unknown payload adapter {name!r} in app/payload.yml. Choose from: {choices}")
    return adapter
