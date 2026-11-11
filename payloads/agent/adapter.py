"""The built-in ``agent`` App payload adapter (issue #338).

Wraps the agent payload's existing runtime-config resolution -- reading
the brick-default + instance ``app.yaml``, substituting ``${var.*}``, and
computing the wheel extras the container needs -- behind the framework's
:class:`~agent_foundry.deploy.payload_adapters.AppPayloadAdapter` seam.

This is the agent-specific half of the App-host seam (issue #337): the
adapter produces the neutral :class:`AppPayloadSpec`; the generic engine
in :mod:`agent_foundry.deploy.app_host` consumes it.  The payload
``spec`` is the single source for the deploy-time runtime toggles
(MCP / A2A / file-upload / charts / feedback) -- they override the
brick-default ``app.yaml`` env via
:func:`payloads.agent.deploy.config.env_overrides_from_spec`.  The
adapter also publishes the agent's spec-derived variables (LLM endpoint,
Lakebase project name, chat-history / file-upload toggles) into the
variable bag, since the agent-agnostic framework no longer supplies
them.

The deploy-time spec/config/lakebase resolution lives in
``payloads/agent/deploy`` (issue #338) and is imported lazily so the
framework adapter registry stays free of SDK imports at load time.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from agent_foundry._instance_layout import has_payload
from agent_foundry.deploy.app_host import AppInfraSpec, AppPayloadSpec
from agent_foundry.scaffold_spec import ScaffoldDecl, ScaffoldSource

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

    from agent_foundry.bricks import (
        BrickResult,
        DeployContext,
        DestroyContext,
        DestroyPlanItem,
    )
    from agent_foundry.smoke import SmokeProbe, SmokeTarget

# Root of the agent payload package -- scaffold sources live under it
# (``_scaffold/`` for the manifest, ``deploy/<cap>/_scaffold/`` for the
# skills / MCP capability trees).  Resolved once at import time.
_PAYLOAD_PKG_ROOT: Path = Path(__file__).resolve().parent


def _seed_runtime_variables(ctx: DeployContext | DestroyContext) -> None:
    """Publish spec-derived agent variables into ``ctx.variables``.

    ``spec.ui.chat_history.enabled`` (and the other agent-owned
    toggles) is the source of truth after #338.  Provision already
    seeded this bag; destroy / plan_destroy must too, otherwise
    ``chat_history_enabled`` defaults to ``"true"`` and Lakebase
    teardown runs even when the spec disabled the capability
    (issue #469).
    """
    from payloads.agent.config import resolve_agent_spec
    from payloads.agent.deploy.config import agent_runtime_variables

    agent_spec = resolve_agent_spec(ctx.instance_dir, variables=ctx.variables)
    ctx.variables.update(agent_runtime_variables(agent_spec, ctx.variables))


class AgentPayloadAdapter:
    """``payload.adapter = agent``: the bundled agent + chat UI payload."""

    name = "agent"

    def applies_to(self, instance_dir: Path, config: Mapping[str, object]) -> bool:  # noqa: ARG002
        # The agent payload runs whenever the instance carries an agent
        # payload surface (``payloads/agent/``).
        return has_payload(instance_dir, self.name)

    def build_payload_spec(
        self,
        instance_dir: Path | None,
        variables: Mapping[str, str],
        spec: Mapping[str, object],
    ) -> AppPayloadSpec:
        from payloads.agent.config import resolve_agent_spec
        from payloads.agent.deploy.config import agent_runtime_variables, wheel_extras_from_spec
        from payloads.agent.deploy.spec import _resolve_app_payload

        agent_spec = resolve_agent_spec(instance_dir, spec, variables=variables)
        vars_dict = dict(variables)
        vars_dict.update(agent_runtime_variables(agent_spec, vars_dict))
        return _resolve_app_payload(
            instance_dir,
            vars_dict,
            requirements_extras=wheel_extras_from_spec(agent_spec),
            spec=agent_spec,
        )

    def build_infra(
        self,
        instance_dir: Path | None,
        variables: Mapping[str, str],
        *,
        target: str,
    ) -> AppInfraSpec:
        """Map ``app/config.yml`` into the neutral :class:`AppInfraSpec`.

        Applies the agent-specific resource gating on top: drops the
        Lakebase ``postgres`` resource when chat history is off or its
        database path is undiscovered, swaps the serving-endpoint grant for
        a model-service grant when the model is a Unity Gateway service,
        and adds the data universe function grant when that block is enabled.
        """
        from agent_foundry.deploy.app_host import build_app_resources
        from payloads.agent.config import resolve_agent_spec
        from payloads.agent.deploy.config import (
            _instance_description,
            _load_app_config,
            agent_runtime_variables,
            data_universe_app_resources,
            filter_app_resources,
            gateway_user_scopes,
            model_app_resources,
            speech_app_resources,
        )

        agent_spec = resolve_agent_spec(instance_dir, variables=variables)
        vars_dict = dict(variables)
        vars_dict.update(agent_runtime_variables(agent_spec, vars_dict))
        app_cfg = _load_app_config(instance_dir, vars_dict)
        raw_resources = filter_app_resources(app_cfg.get("app_resources", []), agent_spec, vars_dict)
        raw_resources = model_app_resources(raw_resources, agent_spec)
        raw_resources += data_universe_app_resources(agent_spec, vars_dict)
        raw_resources += speech_app_resources(agent_spec)
        return AppInfraSpec(
            description=_instance_description(instance_dir),
            app_resources=build_app_resources(raw_resources),
            user_scopes=gateway_user_scopes(app_cfg.get("user_authorization", []), agent_spec),
            sp_grants=app_cfg.get("sp_grants", []),
            compute_size=app_cfg.get("compute_size", "medium"),
            telemetry_destination=app_cfg.get("telemetry_destination"),
        )

    def after_reconcile(
        self,
        ws: WorkspaceClient | None,
        app_name: str,
        variables: Mapping[str, str],
        *,
        dry_run: bool,
    ) -> None:
        """Grant the App SP its Unity Gateway resources; heal its Lakebase access.

        Runs after :meth:`provision`, which has already seeded the
        ``sp``-pinned gateway services, ``chat_history_enabled`` and the
        Lakebase paths into the shared variable bag, so this gates on
        those variables directly.
        """
        from payloads.agent.deploy.gateway_grants import (
            GATEWAY_SP_MCP_SERVICES_VAR,
            GATEWAY_SP_SKILL_SCHEMAS_VAR,
            GATEWAY_SP_SKILLS_VAR,
            ensure_app_sp_gateway_grants,
            split_fqns,
        )

        vars_dict = dict(variables)
        ensure_app_sp_gateway_grants(
            ws,
            app_name,
            split_fqns(vars_dict.get(GATEWAY_SP_MCP_SERVICES_VAR, "")),
            skills=split_fqns(vars_dict.get(GATEWAY_SP_SKILLS_VAR, "")),
            skill_schemas=split_fqns(vars_dict.get(GATEWAY_SP_SKILL_SCHEMAS_VAR, "")),
            dry_run=dry_run,
        )
        chat_history_on = vars_dict.get("chat_history_enabled", "true") == "true"
        if not (chat_history_on and vars_dict.get("lakebase_database_path")):
            return
        from payloads.agent.deploy.lakebase import ensure_app_sp_lakebase_access

        ensure_app_sp_lakebase_access(ws, app_name, vars_dict, dry_run=dry_run)

    def provision(self, ctx: DeployContext) -> int:
        """Run the payload's provision manifest in order (issue #338).

        First publishes the agent's spec-derived variables (LLM endpoint,
        Lakebase project name, chat-history / file-upload toggles) into
        ``ctx.variables`` so the App deploy + Lakebase step can substitute
        them -- the agent-agnostic framework no longer supplies these.
        Then iterates
        :data:`~payloads.agent.deploy.assembly.PROVISION_STEPS` (skills,
        then Lakebase chat history); each step mutates ``ctx.variables``
        in place.  Returns the first non-zero step return code
        (fail-fast: a failed provision step blocks the App deploy).
        A Unity Gateway source skips its local step, and authored content
        that source would ignore fails the provision.
        """
        from payloads.agent.config import resolve_agent_spec
        from payloads.agent.deploy.assembly import active_provision_steps
        from payloads.agent.deploy.gateway_reach import warn_unreachable_brick_skills
        from payloads.agent.deploy.sources import warn_deprecated_mcp_source, warn_deprecated_skills_fields

        errors = self.validate_instance(ctx.instance_dir)
        if errors:
            for error in errors:
                print(f"  ERROR: {error}")
            return 1

        _seed_runtime_variables(ctx)
        spec = resolve_agent_spec(ctx.instance_dir, variables=ctx.variables)
        warn_deprecated_mcp_source(spec)
        warn_deprecated_skills_fields(spec)
        warn_unreachable_brick_skills(ctx.instance_dir, spec, ctx.variables)

        for step in active_provision_steps(resolve_agent_spec(ctx.instance_dir)):
            rc = step.provision(ctx)
            if rc != 0:
                return rc
        return 0

    def validate_instance(self, instance_dir: Path) -> list[str]:
        """Offline config errors for ``agent-foundry validate`` (and ``provision``)."""
        from payloads.agent.config import resolve_agent_spec
        from payloads.agent.deploy.sources import dormant_source_dirs, duplicate_skill_names

        spec = resolve_agent_spec(instance_dir)
        return dormant_source_dirs(instance_dir, spec) + duplicate_skill_names(instance_dir, spec)

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]:
        """Aggregate each provision step's teardown plan in reverse order."""
        from payloads.agent.deploy.assembly import PROVISION_STEPS

        _seed_runtime_variables(ctx)
        items: list[DestroyPlanItem] = []
        for step in reversed(PROVISION_STEPS):
            items.extend(step.plan_destroy(ctx))
        return items

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        """Tear down each provision step in reverse provisioning order."""
        from payloads.agent.deploy.assembly import PROVISION_STEPS

        _seed_runtime_variables(ctx)
        results: list[BrickResult] = []
        for step in reversed(PROVISION_STEPS):
            results.extend(step.destroy(ctx))
        return results

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        """Republish the agent payload's cached Lakebase chat-history paths.

        So a re-deploy with ``chat_history`` toggled off then on reuses
        the cached project / branch / database without reprovisioning.
        Empty when chat history was never enabled (no state).
        """
        from payloads.agent.deploy.chat_history import LAKEBASE_STATE_KEYS, load_lakebase_state

        outputs: dict[str, str] = {}
        lakebase_state = load_lakebase_state(ctx.instance_dir, ctx.target, ctx.ws)
        for key, var_name in LAKEBASE_STATE_KEYS.items():
            if key in lakebase_state:
                outputs[var_name] = str(lakebase_state[key])
        return outputs

    def scaffolds(self) -> Sequence[ScaffoldDecl]:
        """Declare the agent payload's authoring surfaces (issue #368).

        The payload owns everything it writes under
        ``instances/<name>/payloads/agent/`` -- its manifest plus the
        skills and MCP capability trees.  Sources live inside this
        package (``_scaffold/`` for the manifest, ``deploy/<cap>/_scaffold/``
        for the capabilities), so the framework composes these verbs
        without hard-coding any agent path.
        """
        dest = f"payloads/{self.name}"
        skills_scaffold = _PAYLOAD_PKG_ROOT / "deploy" / "skills" / "_scaffold"
        return (
            ScaffoldDecl(
                name="payload",
                group="agent payload",
                display="Agent Payload",
                summary="The agent runtime + chat UI manifest (payload.yml): LLM endpoint, tools, UI toggles.",
                sources=(
                    ScaffoldSource(
                        source=_PAYLOAD_PKG_ROOT / "_scaffold" / "payload.yml",
                        destination=f"{dest}/payload.yml",
                        shape="file",
                    ),
                ),
            ),
            ScaffoldDecl(
                name="skills",
                group="agent payload",
                display="Skills",
                summary="Agent skills (SKILL.md: name, description, instructions) assembled into the system prompt.",
                # The directory name is the skill id and must equal the
                # scaffold's frontmatter ``name``.
                sources=(
                    ScaffoldSource(
                        source=skills_scaffold / "skill",
                        destination=f"{dest}/skills/my-skill",
                        shape="tree",
                    ),
                ),
            ),
            ScaffoldDecl(
                name="mcp",
                group="agent payload",
                display="MCP Servers",
                summary="Allowlist of MCP servers — controls what additional tools the agent can discover.",
                sources=(
                    ScaffoldSource(
                        source=_PAYLOAD_PKG_ROOT / "deploy" / "mcp" / "_scaffold",
                        destination=f"{dest}/mcp_servers/example_server",
                        shape="tree",
                    ),
                ),
            ),
            ScaffoldDecl(
                name="speech_input",
                group="agent payload",
                display="Speech Input",
                summary="Whisper speech-to-text endpoint for the chat UI mic button (Model Serving brick, one T4).",
                # Lands outside payloads/agent/: the Model Serving brick deploys it.
                sources=(
                    ScaffoldSource(
                        source=_PAYLOAD_PKG_ROOT / "deploy" / "speech" / "_scaffold",
                        destination="model_serving/speech",
                        shape="tree",
                    ),
                ),
            ),
        )

    def smoke_probes(self, target: SmokeTarget) -> list[SmokeProbe]:
        """``/invocations`` plus MCP / A2A when the spec exposes them (``agent-foundry smoke``)."""
        from payloads.agent.smoke import agent_smoke_probes

        return agent_smoke_probes(target)
