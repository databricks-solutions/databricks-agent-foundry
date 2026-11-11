"""Register the system prompt in the MLflow Prompt Registry and resolve the deployer."""

from __future__ import annotations

import logging
from typing import Any

from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)


_prompt_info: dict[str, str] = {}


def register_prompt_and_model(
    config: dict[str, Any],
    variables: dict[str, str],
) -> None:
    """Register the system prompt in the MLflow Prompt Registry and set the active model.

    Idempotent — registration runs once per process; the cached prompt
    name/version is injected into every ``config`` dict so traced
    handlers can link the prompt to the active trace.
    """
    if _prompt_info:
        config.update(_prompt_info)
        return

    app_name = variables.get("app_name", "agent")
    catalog = variables.get("catalog", "")
    schema_prefix = variables.get("schema_prefix", "")

    try:
        import mlflow

        mlflow.set_active_model(name=app_name)
        logger.info("[tracing] active model set to '%s'", app_name)

        system_prompt = config.get("SYSTEM_PROMPT", "")
        if system_prompt and catalog and schema_prefix:
            prompt_name = f"{catalog}.{schema_prefix}_agent.{app_name}_system_prompt"
            prompt = _get_or_register_prompt(mlflow, prompt_name, system_prompt)
            if prompt:
                prompt_uri = f"prompts:/{prompt.name}/{prompt.version}"
                _prompt_info["PROMPT_NAME"] = prompt.name
                _prompt_info["PROMPT_VERSION"] = str(prompt.version)
                _prompt_info["PROMPT_URI"] = prompt_uri
                config.update(_prompt_info)
                logger.info(
                    "[tracing] prompt %s v%s (uri=%s)",
                    prompt.name,
                    prompt.version,
                    prompt_uri,
                )
    except Exception as exc:
        logger.warning("[tracing] prompt/model registration failed: %s", exc)


def _get_or_register_prompt(mlflow_mod: Any, prompt_name: str, template: str) -> Any:
    """Return the existing prompt version if the template is unchanged, else register a new one."""
    existing = None
    try:
        existing = mlflow_mod.genai.load_prompt(f"prompts:/{prompt_name}@latest")
        if existing.template == template:
            logger.info("[tracing] prompt unchanged, reusing %s v%s", existing.name, existing.version)
            return existing
    except Exception as exc:
        logger.debug("[tracing] load_prompt failed (may not exist yet): %s", exc)

    try:
        prompt = mlflow_mod.genai.register_prompt(
            name=prompt_name,
            template=template,
            commit_message="auto-registered by agent startup",
        )
    except Exception as exc:
        if existing is not None:
            logger.warning(
                "[tracing] cannot update prompt (template changed) — reusing %s v%s: %s",
                existing.name,
                existing.version,
                exc,
            )
            return existing
        raise

    try:
        mlflow_mod.genai.set_prompt_alias(
            name=prompt_name,
            alias="latest",
            version=prompt.version,
        )
    except Exception as exc:
        logger.debug("[tracing] set_prompt_alias failed: %s", exc)
    return prompt


def resolve_deployer(ws: WorkspaceClient, variables: dict[str, str]) -> None:
    """Auto-resolve ``deployer`` from the SDK's current user if not set."""
    if variables.get("deployer"):
        return
    try:
        # ``User.user_name`` is ``str | None`` in the SDK stubs;
        # fall back to "" so ``variables: dict[str, str]`` stays valid.
        variables["deployer"] = ws.current_user.me().user_name or ""
        logger.info("[config] resolved deployer: %s", variables["deployer"])
    except Exception as exc:
        logger.warning("[config] could not resolve deployer: %s", exc)
