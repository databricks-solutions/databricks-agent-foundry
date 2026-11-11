"""Agent payload deploy-time config glue (issue #338).

Reads and validates the agent payload's per-instance config -- the
``app/config.yml`` infra declaration (compute / resources / OBO scopes /
SP grants) and the ``app/payload.yml`` adapter selection -- and the
agent-specific resource gating (drop the Lakebase ``postgres`` resource
when chat history is off).

Relocated from ``bricks/app/scripts/deploy_agent_app.py`` so the agent
payload owns its deploy glue.  The brick-default packaged data (``bricks/app/app.yaml`` and
``bricks/app/_scaffold/config.yml``) still ships with the App brick and
is read via :mod:`importlib.resources`.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import resources as _resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from agent_foundry.contracts.agent_app import AgentAppConfig
from agent_foundry.loader import FoundryConfigError, read_instance_file
from payloads.agent.config import (
    AgentConfig,
    agent_config_kwargs_from_spec,
)
from payloads.agent.contracts import AgentPayloadSpec
from payloads.agent.deploy.gateway_grants import (
    GATEWAY_SP_MCP_SERVICES_VAR,
    GATEWAY_SP_SKILL_SCHEMAS_VAR,
    GATEWAY_SP_SKILLS_VAR,
    sp_discovery_schemas,
    sp_pinned_fqns,
)
from payloads.agent.runtime.data_universe import qualified_function_name


def resolve_app_config_dir(instance_dir: Path) -> Path:
    """Return the App brick's per-instance config directory, ``app/``.

    A leftover pre-#338 ``agent_app/`` directory raises rather than being
    silently ignored.
    """
    legacy_dir = instance_dir / "agent_app"
    if legacy_dir.is_dir():
        raise FoundryConfigError(f"{legacy_dir} is no longer read; rename it to 'app/' (issue #338).")
    return instance_dir / "app"


def _load_brick_default_config_text() -> str | None:
    """Read ``bricks/app/_scaffold/config.yml`` from package data.

    Single source of truth: this same file backs ``agent-foundry scaffold
    agent_app`` (see ``agent_foundry/scaffold.py``).  Keeping one file
    instead of a separate ``defaults/`` copy guarantees the scaffold
    output is exactly what the framework will read at deploy time.

    See ``payloads.agent.deploy.spec._load_brick_app_yaml_text`` for the
    importlib.resources rationale (issue #81).
    """
    try:
        ref = _resources.files("bricks.app").joinpath(
            "_scaffold",
            "config.yml",
        )
        return ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError):
        return None


def _load_app_config(
    instance_dir: Path | None,
    variables: dict[str, str],
) -> dict[str, Any]:
    """Load agent_app config with brick-default + instance-override merge.

    1. Load ``bricks/app/_scaffold/config.yml`` (framework wiring).
    2. If the instance has ``app/config.yml``, shallow-merge its
       top-level keys over the brick default (instance replaces any key
       it specifies).
    3. Validate the merged dict against :class:`AgentAppConfig` -- a
       typo'd key or an invalid ``compute_size`` fails fast here.
    4. Substitute ``${var.*}`` in the merged result.
    """
    data: dict[str, Any] = {}
    brick_cfg_text = _load_brick_default_config_text()
    if brick_cfg_text is not None:
        data = yaml.safe_load(brick_cfg_text) or {}

    if instance_dir:
        instance_cfg_path = resolve_app_config_dir(instance_dir) / "config.yml"
        if instance_cfg_path.exists():
            overrides = yaml.safe_load(instance_cfg_path.read_text(encoding="utf-8")) or {}
            data.update(overrides)

    if not data:
        return {}

    try:
        data = AgentAppConfig.model_validate(data).model_dump()
    except ValidationError as exc:
        raise FoundryConfigError(f"Invalid app/config.yml: {exc}") from exc

    from agent_foundry.variables import substitute_obj

    substituted: dict[str, Any] = substitute_obj(data, dict(variables), strict=False)
    return substituted


def _bool_env(value: bool) -> str:
    """Render a toggle as the ``"true"``/``"false"`` env strings."""
    return "true" if value else "false"


def _days_env(days: int | None) -> str:
    """Render an optional day count; ``"0"`` means off."""
    return str(days or 0)


def env_overrides_from_spec(spec: AgentPayloadSpec) -> dict[str, str]:
    """Derive the App's runtime-toggle env from the agent ``spec``.

    The agent payload's ``spec`` is the single source for the deploy-time
    toggles.  Mount/exposure toggles map 1:1 to ``ENABLE_*`` env the App
    container reads; ``FILE_UPLOAD_MAX_MB`` carries the size cap,
    ``TOOL_OUTPUT_EXPANDED`` the tool-panel presentation mode,
    ``CHAT_MAX_WIDTH`` the chat column's width cap and
    ``CHAT_HISTORY_*_DAYS`` the sidebar window and Lakebase retention,
    ``USER_SKILLS_*`` the Lakebase user-skill store and its limits,
    ``COMPACTION_*`` conversation compaction with its derived trigger,
    ``*_PLAN``/``*_AGENT_MEMORY*`` the agent plan and long-term memory, and
    ``*SPEECH_*`` the composer mic button and its transcription endpoint.
    """
    ui = spec.ui
    integrations = spec.integrations
    user_skills = spec.runtime.tools.skills.user_skills
    context = spec.runtime.context
    return {
        "ENABLE_MCP_SERVER": _bool_env(integrations.mcp_server.enabled),
        "ENABLE_A2A": _bool_env(integrations.a2a.enabled),
        "ENABLE_FILE_UPLOAD": _bool_env(ui.file_upload.enabled),
        "FILE_UPLOAD_MAX_MB": str(ui.file_upload.max_mb),
        "ENABLE_IMAGE_INPUT": _bool_env(ui.file_upload.enabled and ui.file_upload.images),
        "ENABLE_CHARTS": _bool_env(ui.charts.enabled),
        "ENABLE_FEEDBACK": _bool_env(ui.feedback.enabled),
        "TOOL_OUTPUT_EXPANDED": _bool_env(ui.tool_output.expanded),
        "CHAT_MAX_WIDTH": ui.chat_layout.width,
        "ENABLE_RESOURCES_SIDEBAR": _bool_env(ui.resources_sidebar.enabled),
        "CHAT_HISTORY_SIDEBAR_DAYS": _days_env(ui.chat_history.sidebar_days),
        "CHAT_HISTORY_RETENTION_DAYS": _days_env(ui.chat_history.retention_days),
        "ENABLE_USER_SKILLS": _bool_env(spec.runtime.tools.skills.lakebase_user_skills() is not None),
        "USER_SKILLS_MAX": str(user_skills.max_skills),
        "USER_SKILLS_MAX_MD_CHARS": str(user_skills.max_skill_md_chars),
        "ENABLE_COMPACTION": _bool_env(spec.compaction_enabled()),
        "COMPACTION_TRIGGER_TOKENS": str(context.compaction_trigger_input_tokens()),
        "COMPACTION_KEEP_TURNS": str(context.compaction.keep_recent_turns),
        "COMPACTION_INSTRUCTIONS": context.compaction.instructions or "",
        "COMPACTION_MANUAL_ACTION": _bool_env(spec.compaction_enabled() and context.compaction.manual_action),
        "ENABLE_CHAT_PLAN": _bool_env(spec.plan_enabled()),
        "PLAN_MAX_CHARS": str(context.plan.max_chars),
        "ENABLE_AGENT_MEMORY": _bool_env(spec.memory_enabled()),
        "AGENT_MEMORY_MAX_TOPIC_CHARS": str(context.memory.max_entry_chars),
        "ENABLE_SPEECH_INPUT": _bool_env(ui.speech_input.enabled),
        "SPEECH_BACKEND": ui.speech_input.backend,
        "SPEECH_ENDPOINT": speech_endpoint(spec),
        "SPEECH_MAX_SECONDS": str(ui.speech_input.max_seconds),
        "SPEECH_LANGUAGE": ui.speech_input.language or "",
    }


def wheel_extras_from_spec(spec: AgentPayloadSpec) -> tuple[str, ...]:
    """Wheel extras the App container must install for the agent payload.

    The base ``runtime`` extra is always required; file upload adds the
    out-of-process extractor's ``extraction`` deps so
    fastapi/uvicorn/openpyxl/... import in the container.
    """
    if spec.ui.file_upload.enabled:
        print("  File upload enabled: staging will install the 'extraction' extra.")
        return ("runtime", "extraction")
    return ("runtime",)


def agent_runtime_variables(
    spec: AgentPayloadSpec,
    variables: Mapping[str, str],
) -> dict[str, str]:
    """Agent-derived variables the (agent-agnostic) framework no longer supplies.

    The framework's :meth:`InstanceConfig.to_variables` used to flatten
    agent specifics (LLM endpoint, Lakebase project name, chat-history /
    file-upload toggles) into the variable bag.  The agent payload now
    owns those: this maps its ``spec`` into the same variable keys that
    ``app.yaml`` substitution and the Lakebase provision step consume.
    """
    agent = AgentConfig(**agent_config_kwargs_from_spec(spec))
    chat_history = spec.ui.chat_history
    app_name = variables.get("app_name", "")
    target = variables.get("target_name", "")
    derived_lakebase = f"{target}-{app_name}-lakebase" if app_name and target else ""
    return {
        "llm_endpoint_name": agent.llm_endpoint,
        "chat_history_enabled": _bool_env(chat_history.enabled),
        "lakebase_project_name": chat_history.project_name or derived_lakebase,
        "file_upload_enabled": _bool_env(spec.ui.file_upload.enabled),
        "file_upload_max_mb": str(spec.ui.file_upload.max_mb),
        GATEWAY_SP_MCP_SERVICES_VAR: ",".join(sp_pinned_fqns(agent.gateway_mcp)),
        GATEWAY_SP_SKILLS_VAR: ",".join(sp_pinned_fqns(agent.gateway_skills)),
        GATEWAY_SP_SKILL_SCHEMAS_VAR: ",".join(sp_discovery_schemas(agent.gateway_skills)),
    }


GATEWAY_OBO_SCOPE = "ai-gateway"


def gateway_user_scopes(scopes: list[str], spec: AgentPayloadSpec) -> list[str]:
    """Add the ``ai-gateway`` OBO scope when the agent mounts any Unity Gateway resource."""
    tools = spec.runtime.tools
    uses_gateway = tools.mcp.effective_gateway() is not None or tools.skills.effective_gateway() is not None
    if not uses_gateway or GATEWAY_OBO_SCOPE in scopes:
        return scopes
    return [*scopes, GATEWAY_OBO_SCOPE]


def chat_history_postgres_enabled(
    spec: AgentPayloadSpec,
    variables: Mapping[str, str],
) -> bool:
    """Return True when the App should bind the Lakebase ``postgres`` resource."""
    if not spec.ui.chat_history.enabled:
        return False
    return bool(variables.get("lakebase_database_path"))


def filter_app_resources(
    raw_resources: list[dict[str, Any]],
    spec: AgentPayloadSpec,
    variables: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Drop the ``postgres`` resource when chat history is off or Lakebase paths are missing."""
    if chat_history_postgres_enabled(spec, variables):
        return raw_resources
    if spec.ui.chat_history.enabled and not variables.get("lakebase_database_path"):
        print(
            "  WARNING: Skipping postgres app_resource -- "
            "lakebase database path was not discovered (chat history disabled for this deploy)"
        )
    return [r for r in raw_resources if r.get("name") != "postgres"]


def model_app_resources(
    raw_resources: list[dict[str, Any]],
    spec: AgentPayloadSpec,
) -> list[dict[str, Any]]:
    """Grant the app's SP the agent's model, as the kind the spec names.

    A serving endpoint keeps the brick-default ``serving_endpoint``
    resource.  A Unity Gateway model service replaces it with a
    ``MODEL_SERVICE`` ``EXECUTE`` securable: the substituted endpoint
    resource would otherwise point at an FQN that is not an endpoint.
    """
    agent = AgentConfig(**agent_config_kwargs_from_spec(spec))
    if agent.llm_kind != "model_service":
        return raw_resources
    kept = [r for r in raw_resources if (r.get("serving_endpoint") or {}).get("name") != agent.llm_endpoint]
    return [
        *kept,
        {
            "name": "llm-model-service",
            "uc_securable": {
                "securable_full_name": agent.llm_endpoint,
                "securable_type": "MODEL_SERVICE",
                "permission": "EXECUTE",
            },
        },
    ]


def speech_app_resources(spec: AgentPayloadSpec) -> list[dict[str, Any]]:
    """Grant the app's SP ``CAN_QUERY`` on the speech endpoint when the mic button is on.

    Derived rather than authored in ``app/config.yml`` for the same reason
    as :func:`data_universe_app_resources`: that file replaces the resource
    list wholesale, and the endpoint already lives in ``payload.yml``.
    """
    if not spec.ui.speech_input.enabled:
        return []
    return [{"name": "speech-endpoint", "serving_endpoint": {"name": speech_endpoint(spec), "permission": "CAN_QUERY"}}]


def speech_endpoint(spec: AgentPayloadSpec) -> str:
    """The speech endpoint name, refusing a ``${var.*}`` placeholder left unresolved.

    Unresolved, the app would call a missing endpoint and the resource
    builder would silently drop the ``CAN_QUERY`` grant.
    """
    speech = spec.ui.speech_input
    if speech.enabled and "${var." in speech.endpoint:
        raise ValueError(
            f"spec.ui.speech_input.endpoint {speech.endpoint!r} is unresolved: "
            "deploy the model_serving sub-instance it names first"
        )
    return speech.endpoint


def data_universe_app_resources(
    spec: AgentPayloadSpec,
    variables: Mapping[str, str],
) -> list[dict[str, Any]]:
    """Grant the app's SP ``EXECUTE`` on the injected overview function.

    The runtime calls this function as itself on the request path, so the
    grant has to exist before the first request.  It is derived here rather
    than authored in an instance's ``app/config.yml`` for two reasons: that
    file replaces the framework's resource list wholesale, and the function
    name already lives in ``payload.yml`` -- a second copy is a second thing
    to keep in sync.
    """
    agent = AgentConfig(**agent_config_kwargs_from_spec(spec))
    if not agent.data_universe_enabled:
        return []

    securable = qualified_function_name(agent.data_universe_function, variables)
    if not securable:
        print(
            "  WARNING: Skipping the data universe app_resource -- "
            f"cannot qualify function {agent.data_universe_function!r} without an agent_schema"
        )
        return []
    return [
        {
            "name": "data-universe-function",
            "uc_securable": {
                "securable_full_name": securable,
                "securable_type": "FUNCTION",
                "permission": "EXECUTE",
            },
        }
    ]


def _instance_description(instance_dir: Path | None) -> str:
    """Resolve the App description from the instance's ``foundry.toml`` (agent default)."""
    cfg = read_instance_file(instance_dir) if instance_dir else None
    return (cfg.description if cfg else "") or "Agent Bricks bundled agent + chat UI"
