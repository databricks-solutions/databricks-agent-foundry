"""
MLflow Tracing Setup
=====================
Wires the OTEL trace destination at module-load time using env vars
populated by the App container's ``app.yaml``.  Tracing is **always
on** in App deploys -- the ``@invoke()`` / ``@stream()`` decorators
plus ``mlflow.openai.autolog()`` produce spans regardless of any
agent-level config flag.  This module configures *where* those spans
go, disables the duplicate Agents SDK tracer autolog would otherwise
install, and stamps ``mlflow.llm.provider`` from the serving client.

Environment variables consumed (all set in the brick-default
``bricks/app/app.yaml``):

* ``MLFLOW_EXPERIMENT_ID`` -- the experiment that owns the traces.
* ``TRACING_UC_DESTINATION`` -- ``catalog.schema[.table_prefix]`` for
  the OTEL Delta tables.  When omitted, traces fall back to routing
  through the MLflow tracking server.
* ``MLFLOW_TRACING_SQL_WAREHOUSE_ID`` -- read by MLflow's UC-trace
  query path; set in ``app.yaml`` so feedback / search-traces work.

Customers who need GDPR redaction set ``MLFLOW_TRACE_REDACT_INPUTS``
and ``MLFLOW_TRACE_REDACT_OUTPUTS`` in their ``app.yaml`` directly;
the framework has no redaction setting of its own.

Customers who genuinely need to disable tracing in their own deploy
should call :func:`mlflow.tracing.disable` from their startup hook.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_early_tracing_done = False
_llm_provider: str | None = None


def llm_provider_for_client(client: Any) -> str:
    """Serving-adapter identity for ``mlflow.llm.provider``.

    openai autolog never sets that attribute. MLflow's cost UI only
    infers ``databricks`` from a ``databricks-`` model-name prefix, so
    gateway-resolved names (``global.anthropic.*``) land as Provider
    NULL. Stamp from the client class, not from a model-name map.
    """
    try:
        from databricks_openai import AsyncDatabricksOpenAI

        openai_cls: Any = AsyncDatabricksOpenAI
    except ImportError:
        openai_cls = ()
    if openai_cls and isinstance(client, openai_cls):
        return "databricks"
    return "openai"


def stamp_chat_model_provider(span: Any) -> None:
    """Fill ``mlflow.llm.provider`` on CHAT_MODEL spans that lack one."""
    if _llm_provider is None:
        return
    from mlflow.entities import SpanType
    from mlflow.tracing.constant import SpanAttributeKey

    if span.span_type != SpanType.CHAT_MODEL:
        return
    if span.get_attribute(SpanAttributeKey.MODEL_PROVIDER):
        return
    span.set_attribute(SpanAttributeKey.MODEL_PROVIDER, _llm_provider)


def _install_openai_autolog() -> None:
    from mlflow.openai import autolog as openai_autolog

    openai_autolog()  # type: ignore[no-untyped-call]


def _disable_agents_sdk_tracer() -> None:
    from agents import set_tracing_disabled

    set_tracing_disabled(True)


def configure_openai_autolog(client: Any) -> None:
    """Install openai autolog, drop the duplicate Agents SDK tracer, stamp provider.

    ``mlflow.openai.autolog()`` patches HTTP completions *and* registers
    ``MlflowOpenAgentTracingProcessor``. The latter emits a sibling
    ``Generation`` CHAT_MODEL span. MLflow's cost breakdown then
    DFS-dedups ``tokenUsage`` onto the parent and drops Databricks cache
    extras that only exist on the Completions span.

    ``set_tracing_disabled(True)`` turns off the Agents SDK tracer so
    one CHAT_MODEL span remains — the HTTP one autolog already records.
    """
    global _llm_provider

    from mlflow.tracing import configure as configure_tracing
    from mlflow.tracing.config import get_config

    _install_openai_autolog()
    _disable_agents_sdk_tracer()

    _llm_provider = llm_provider_for_client(client)
    processors = list(get_config().span_processors)
    if stamp_chat_model_provider not in processors:
        configure_tracing(span_processors=[*processors, stamp_chat_model_provider])
    logger.info("[tracing] openai autolog on; agents SDK tracer off; llm.provider=%s", _llm_provider)


def setup_tracing_early() -> None:
    """Configure the trace destination at module-load time using env vars.

    The ``@stream()`` / ``@invoke()`` MLflow decorators create the trace
    span *before* the handler body runs, so the destination must already
    be active when the first request arrives.

    Uses ``set_experiment(trace_location=UnityCatalog(...))`` which both
    registers traces in the tracking server (needed for feedback/assessments)
    AND writes OTEL spans to the UC Delta tables.

    Requires ``MLFLOW_EXPERIMENT_ID`` and ``TRACING_UC_DESTINATION``
    (``catalog.schema``) env vars, both set in the app's ``app.yaml``.

    Call once at import time from ``agent.py``.
    """
    global _early_tracing_done
    if _early_tracing_done:
        return
    _early_tracing_done = True

    from agent_foundry import env

    experiment_id = env.read_optional(env.MLFLOW_EXPERIMENT_ID)
    uc_destination = env.read_optional(env.TRACING_UC_DESTINATION) or ""
    if not experiment_id:
        return

    try:
        import mlflow

        mlflow.set_tracking_uri("databricks")

        if uc_destination:
            parsed = _parse_destination(uc_destination)
            if parsed:
                catalog_name, schema_name, table_prefix = parsed
                try:
                    from mlflow.entities.trace_location import UnityCatalog

                    uc_kwargs: dict[str, str] = {
                        "catalog_name": catalog_name,
                        "schema_name": schema_name,
                    }
                    if table_prefix:
                        uc_kwargs["table_prefix"] = table_prefix
                    uc_loc = UnityCatalog(**uc_kwargs)

                    try:
                        mlflow.set_experiment(
                            experiment_id=experiment_id,
                            trace_location=uc_loc,
                        )
                        logger.info("[tracing] early setup: experiment %s → UC %s", experiment_id, uc_destination)
                        return
                    except Exception as exc:
                        if "already linked" in str(exc):
                            _activate_existing_uc_experiment(mlflow, experiment_id, uc_loc)
                            return
                        logger.warning("[tracing] early setup: set_experiment+UC failed (%s), falling back", exc)

                except ImportError:
                    logger.warning("[tracing] early setup: UnityCatalog not available, falling back")

        _set_destination_from_experiment(mlflow, experiment_id)
        logger.info("[tracing] early setup: destination → experiment %s", experiment_id)
    except Exception as exc:
        logger.warning("[tracing] early setup failed: %s", exc)


def _activate_existing_uc_experiment(
    mlflow: Any,
    experiment_id: str,
    uc_location: Any,
) -> None:
    """Handle the case where the experiment is already linked to our UC destination.

    ``set_experiment(trace_location=...)`` raises when the experiment already
    has a matching UC binding (due to a string-comparison quirk across MLflow
    versions).  We work around it by:
    1. Setting the active experiment (without trace_location so it doesn't clear anything).
    2. Manually configuring the OTEL exporter via ``_sync_trace_destination_and_provider``.
    """
    exp = mlflow.set_experiment(experiment_id=experiment_id)
    try:
        from mlflow.tracking.fluent import _sync_trace_destination_and_provider

        resolved = exp.trace_location if exp.trace_location is not None else uc_location
        _sync_trace_destination_and_provider(resolved)
        logger.info("[tracing] early setup: experiment %s → UC (existing binding)", experiment_id)
    except (ImportError, AttributeError):
        logger.warning("[tracing] early setup: experiment %s (UC sync unavailable)", experiment_id)


def _parse_destination(destination: str) -> tuple[str, str, str | None] | None:
    """Split ``catalog.schema[.table_prefix]`` into components.

    Returns ``(catalog, schema, table_prefix)`` where *table_prefix* is
    ``None`` when only ``catalog.schema`` was given.
    """
    parts = destination.split(".")
    if len(parts) == 2:
        return parts[0], parts[1], None
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    logger.warning(
        "[tracing] TRACING_UC_DESTINATION must be 'catalog.schema' or 'catalog.schema.prefix', got: %s",
        destination,
    )
    return None


def _set_destination_from_experiment(mlflow: Any, experiment_id: str) -> None:
    """Fallback: activate the trace exporter via MlflowExperimentLocation.

    Only used when ``TRACING_UC_DESTINATION`` is not set.  Note that
    this routes traces through the MLflow tracking server, which may
    not populate the UC OTEL Delta tables.
    """
    try:
        import mlflow.tracing as _tracing
        from mlflow.entities.trace_location import MlflowExperimentLocation

        _tracing.set_destination(destination=MlflowExperimentLocation(experiment_id=experiment_id))
        logger.info(
            "[tracing] trace destination set to experiment %s",
            experiment_id,
        )
    except ImportError:
        logger.warning("[tracing] MlflowExperimentLocation not available — upgrade to mlflow >= 3.9.0")
    except Exception as exc:
        logger.warning("[tracing] set_destination failed: %s", exc)
