"""Load the runtime variables and agent config, and initialise the Agents SDK once per process."""

from __future__ import annotations

import copy
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from databricks.sdk import WorkspaceClient

from agent_foundry import env
from agent_foundry.workspace_client import skip_default_workspace_client
from payloads.agent.runtime.config import load_agent_config
from payloads.agent.runtime.instance_paths import ensure_instance_on_sys_path, project_root, resolve_instance_dir
from payloads.agent.runtime.prompt_cache import configure_prompt_cache
from payloads.agent.runtime.prompt_registry import register_prompt_and_model, resolve_deployer
from payloads.agent.runtime.request_context import get_workspace_client
from payloads.agent.runtime.tool_result_clearing import configure_tool_result_clearing

logger = logging.getLogger(__name__)

_module_initialized = False
_gateway_client_installed = False


def load_variables() -> dict[str, str]:
    """Load variables from the instance's ``foundry.toml`` + deployment state.

    After merging identity fields and state outputs, computes derived
    values that ``InstanceConfig.to_variables()`` would normally provide
    at deploy time (e.g. ``agent_schema``).
    """
    project_root()  # the runtime requires an explicit project root (issue #143)
    target = env.read(env.DEPLOY_TARGET)
    instance_dir = resolve_instance_dir()
    variables = _instance_identity_variables(instance_dir) if instance_dir else {}
    if instance_dir:
        _preload_runtime_state(instance_dir, target, variables)
    _add_computed_variables(variables)
    return variables


def _instance_identity_variables(instance_dir: Path) -> dict[str, str]:
    """Identity fields authored in ``<instance_dir>/foundry.toml``.

    The repo-root ``foundry.toml`` is per-operator and never staged into
    the App, so target-level values (``catalog``, ...) arrive as env vars
    instead -- see :func:`_add_computed_variables`.
    """
    from agent_foundry.loader import read_instance_file

    cfg = read_instance_file(instance_dir)
    if cfg is None:
        return {}
    identity = {
        "app_name": cfg.app_name,
        "schema_prefix": cfg.schema_prefix,
        "vs_endpoint_name": cfg.vs_endpoint_name,
    }
    return {key: value for key, value in identity.items() if value}


def _preload_runtime_state(
    instance_dir: Path,
    target: str,
    variables: dict[str, str],
) -> None:
    """Merge deployment state outputs into *variables*.

    Reads workspace-backed state via a ``WorkspaceClient`` so the deployed
    agent always sees the current state written by the orchestrator — not
    a stale local cache that may have been bundled with the source sync.
    Adds no state outputs if the SDK cannot authenticate (e.g. local dev
    without env vars).

    Uses ``agent_foundry.load_state_outputs`` which walks the
    registered :class:`~agent_foundry.bricks.Brick` instances to
    aggregate outputs (replaces the old ``STATE_KEY_MAPS`` -- 68-C,
    issue #84).
    """
    from agent_foundry.config import load_state_outputs

    ws = _get_runtime_workspace_client()
    variables.update(load_state_outputs(instance_dir, target, ws=ws).variables)


def _get_runtime_workspace_client() -> WorkspaceClient | None:
    """The App's own client for state lookups, so the process builds no second one.

    ``None`` -- state outputs are then skipped -- under the unit-test skip
    env, where SDK credential discovery could hang, and when construction
    fails, as in local dev without credentials.
    """
    if skip_default_workspace_client():
        return None
    try:
        return get_workspace_client()
    except Exception:
        return None


def _setdefault_from_env(variables: dict[str, str], key: str, var: env.EnvVar) -> None:
    value = env.read_optional(var)
    if value:
        variables.setdefault(key, value)


def _add_computed_variables(variables: dict[str, str]) -> None:
    """Derive values that ``InstanceConfig.to_variables()`` computes at
    deploy time but that aren't present in the instance file or state files.

    Only fills in keys that are missing so explicit overrides are respected.

    ``catalog`` is a target-level value from the repo-root ``foundry.toml``,
    which is not staged into the App.  ``AGENT_CATALOG``,
    ``AGENT_SCHEMA_PREFIX`` and ``AGENT_SCHEMA`` are therefore resolved at
    deploy time in ``bricks/app/app.yaml`` and read back here, so
    ``catalog``, ``schema_prefix`` and ``agent_schema`` are available for
    the ``${var.*}`` in Unity Gateway FQNs.
    """
    _setdefault_from_env(variables, "catalog", env.AGENT_CATALOG)
    _setdefault_from_env(variables, "schema_prefix", env.AGENT_SCHEMA_PREFIX)
    catalog = variables.get("catalog", "")
    schema_prefix = variables.get("schema_prefix", "")
    if catalog and schema_prefix:
        variables.setdefault("agent_schema", f"{catalog}.{schema_prefix}_agent")
        return
    from_env = env.read_optional(env.AGENT_SCHEMA)
    if from_env:
        variables.setdefault("agent_schema", from_env)


# ---------------------------------------------------------------------------
# SDK init
# ---------------------------------------------------------------------------


def init_agent_module() -> None:
    """One-time setup for the OpenAI Agents SDK and MLflow integration.

    Must call ``mlflow.openai.autolog()`` at init time (not per-request)
    so the HTTP completions patch is registered before the first
    ``Runner.run()`` call.  The Agents SDK's own tracer is then disabled
    so MLflow does not emit a duplicate ``Generation`` CHAT_MODEL span.
    """
    global _module_initialized
    if _module_initialized:
        return

    from agents import set_default_openai_api, set_default_openai_client

    from payloads.agent.runtime.prompt_cache import build_llm_client
    from payloads.agent.runtime.tracing import configure_openai_autolog

    # Subclass of ``AsyncDatabricksOpenAI`` that marks the stable prefix of
    # each request with ``cache_control``.  The SDK gives no seam for this
    # higher up: instructions are serialised into a bare system string,
    # which cannot carry a per-block marker.
    client = build_llm_client("serving_endpoint")
    set_default_openai_client(client)
    set_default_openai_api("chat_completions")
    configure_openai_autolog(client)

    _module_initialized = True


def configure_llm_client(config: Mapping[str, Any]) -> None:
    """Point the default chat client at Unity Gateway for a model service.

    :func:`init_agent_module` runs before any config exists, so it always
    installs the serving-endpoint client.  One process serves one
    instance, so the swap happens at most once.
    """
    global _gateway_client_installed
    if config.get("LLM_KIND") != "model_service" or _gateway_client_installed:
        return

    from agents import set_default_openai_client

    from payloads.agent.runtime.prompt_cache import build_llm_client
    from payloads.agent.runtime.tracing import configure_openai_autolog

    client = build_llm_client("model_service")
    set_default_openai_client(client)
    configure_openai_autolog(client)
    _gateway_client_installed = True
    logger.info("LLM client routed through Unity Gateway for model service %s", config.get("LLM_ENDPOINT_NAME"))


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_AGENT_CONFIG_CACHE: tuple[dict[str, Any], dict[str, str]] | None = None


def _load_agent_config_and_vars_uncached(
    ws: WorkspaceClient,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Inner implementation of :func:`load_agent_config_and_vars`.

    Kept separate so tests can exercise the cold path without
    fighting the process-level cache, and so the cached wrapper
    stays a thin one-liner.
    """
    init_agent_module()

    # Put the instance dir on sys.path before anything imports instance code
    # (``type: python`` tool callables and reset/finalize hooks resolve their
    # dotted paths against it).  This seam runs on every entry path -- the
    # @invoke/@stream handlers and the MCP/A2A ``run_agent`` path all load
    # config here before building tools -- and once per process is enough
    # (the insert persists for the process lifetime).
    ensure_instance_on_sys_path()

    instance_dir = resolve_instance_dir()
    variables = load_variables()
    resolve_deployer(ws, variables)
    config = load_agent_config(instance_dir, variables)
    if variables.get("deployer"):
        config["DEPLOYER_USER"] = variables["deployer"]
    register_prompt_and_model(config, variables)
    configure_llm_client(config)
    configure_prompt_cache(config)
    configure_tool_result_clearing(config)

    return config, variables


def load_agent_config_and_vars(
    ws: WorkspaceClient,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Load agent config and resolved variables.

    Returns ``(config, variables)``.

    Process-level memoization
    --------------------------
    Every input to the underlying loader is deploy-time state -- the
    instance YAML, the assembled skills artefact, the workspace state
    files written by the bricks during deploy, and the service
    principal's identity (``ws.current_user.me()``).  None of it
    depends on the request, the OBO user, or the conversation.  So we
    cache ``(config, variables)`` once per process and serve **deep
    copies** to callers on subsequent requests.

    Defensive copy is required: ``stream_handler`` / ``invoke_handler``
    inject ``_resolved_username`` into ``config`` per-request and
    other consumers may also mutate either dict.  Sharing the cached
    references would let a mutation in one request bleed into the
    next.

    Cold-start: the first call still pays the workspace-state read
    (~2.6s observed in production traces).  Warm calls are O(deepcopy)
    -- microseconds.

    Test escape hatch: :func:`_clear_agent_config_cache` resets the
    cache for unit tests that need to exercise the cold path.
    Production code must never call it.
    """
    global _AGENT_CONFIG_CACHE
    if _AGENT_CONFIG_CACHE is None:
        _AGENT_CONFIG_CACHE = _load_agent_config_and_vars_uncached(ws)
    cached_config, cached_variables = _AGENT_CONFIG_CACHE
    return copy.deepcopy(cached_config), copy.deepcopy(cached_variables)


def _clear_agent_config_cache() -> None:
    """Test-only escape hatch.  Production code must never call this."""
    global _AGENT_CONFIG_CACHE
    _AGENT_CONFIG_CACHE = None
