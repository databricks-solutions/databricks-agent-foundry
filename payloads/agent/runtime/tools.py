"""
Databricks Tool Factories
===========================
Creates ``FunctionTool`` instances for the OpenAI Agents SDK.

- **Genie** uses the direct SDK (``ws.genie``) since no dedicated
  tool class exists.
- **Vector Search** uses ``VectorSearchRetrieverTool`` from
  ``databricks_openai``, which auto-discovers index schema and
  column metadata from Unity Catalog.

UC functions are served through the MCP brick; this module only keeps
the SQL statement helpers ``data_universe`` uses to read one.

All are bridged to ``FunctionTool`` via ``on_invoke_tool`` callbacks
wrapped with ``asyncio.to_thread()`` for non-blocking execution.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import importlib.util
import inspect
import json
import logging
import sys
from collections.abc import Callable
from datetime import timedelta
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any, cast

import mlflow
from agents import FunctionTool
from databricks_ai_bridge.vector_search_retriever_tool import FilterItem
from databricks_openai import VectorSearchRetrieverTool

from payloads.agent.runtime.instance_paths import resolve_instance_dir

if TYPE_CHECKING:
    from databricks.sdk import WorkspaceClient

logger = logging.getLogger(__name__)

_GENIE_TIMEOUT = timedelta(minutes=5)

_UC_FUNCTION_INITIAL_WAIT_SECONDS = 50
_UC_FUNCTION_POLL_BACKOFF_CAP_SECONDS = 20.0
_TERMINAL_STATEMENT_STATES: frozenset[str] = frozenset({"SUCCEEDED", "FAILED", "CANCELED", "CLOSED"})


# ---------------------------------------------------------------------------
# Genie (direct SDK — no dedicated tool class exists)
# ---------------------------------------------------------------------------


def create_genie_tool(
    ws: WorkspaceClient,
    space_id: str,
    name: str = "query_genie",
) -> FunctionTool:
    """Build a ``FunctionTool`` that queries a Genie space.

    ``name`` is used both as the MLflow span name and the ``FunctionTool``
    name, so:

    - Multiple Genie tools on the same agent (different ``space_id``) are
      distinguishable in traces and in LLM tool-call logs.
    - ``tool_routing_accuracy`` and other span-name-based scorers can match
      the tool's logical name from the skill config.

    Defaults to ``"query_genie"`` to keep backward compatibility with the
    legacy top-level TOOLS config path that doesn't carry a skill name.
    """

    async def _invoke(_ctx: Any, args_json: str) -> str:
        import mlflow

        args = json.loads(args_json) if args_json else {}
        question = args.get("question", "")
        if not question:
            return "Error: 'question' parameter is required."

        # ``span_type="CHAIN"`` (not ``"TOOL"``) deliberately: the OpenAI
        # Agents SDK already auto-instruments this ``FunctionTool`` with
        # its own TOOL span (see agent-foundry issue #60).  Tagging our
        # wrapper as CHAIN keeps the span visible for debugging without
        # duplicating the TOOL kind that scorers like
        # ``tool_call_correctness`` count -- which previously flagged
        # every Genie row as a redundant tool call.
        with mlflow.start_span(name=name, span_type="CHAIN") as span:
            span.set_inputs({"question": question, "space_id": space_id})
            span.set_attribute("tool_type", "genie")

            logger.info("[genie] querying space %s: %s", space_id, question[:120])
            try:
                msg = await asyncio.to_thread(
                    ws.genie.start_conversation_and_wait,
                    space_id,
                    question,
                    timeout=_GENIE_TIMEOUT,
                )
            except Exception as exc:
                logger.warning("[genie] query failed", exc_info=True)
                return f"Genie query failed: {exc}"

            result, sql = _format_genie_message(ws, msg, space_id)
            outputs: dict[str, Any] = {"result_length": len(result)}
            if sql:
                # Surfaced for ``genie_sql_correctness`` (see
                # bricks/evaluation/scorers/genie_accuracy.py) so dev
                # evaluations can score the SQL directly without
                # parsing the response payload.
                outputs["sql"] = sql
                span.set_attribute("sql", sql)
            span.set_outputs(outputs)
            return result

    return FunctionTool(
        name=name,
        description=(
            "Query a Genie space with a natural-language question about "
            "Unity Catalog metadata — tables, columns, schemas, and data. "
            "Returns the AI-generated answer and any SQL query results."
        ),
        params_json_schema={
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The natural-language question to ask Genie.",
                },
            },
            "required": ["question"],
        },
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )


def _format_genie_message(
    ws: WorkspaceClient,
    msg: Any,
    space_id: str,
) -> tuple[str, str | None]:
    """Extract readable text and the generated SQL from a ``GenieMessage``.

    Returns ``(payload, sql)`` where ``sql`` is the first SQL query
    string carried by any attachment (``None`` when the message has no
    SQL-bearing attachment).  Exposing SQL separately lets the caller
    record it as a structured span output / attribute for downstream
    scorers without re-parsing the rendered payload string.
    """
    parts: list[str] = []
    sql: str | None = None

    if not msg.attachments:
        return msg.content or "(Genie returned no content)", None

    for att in msg.attachments:
        if att.text and att.text.content:
            parts.append(att.text.content)

        if att.query:
            if sql is None:
                sql = att.query.query
            parts.append(f"SQL: {att.query.query}")
            try:
                result = ws.genie.execute_message_attachment_query(
                    space_id,
                    msg.conversation_id,
                    msg.id,
                    att.attachment_id,
                )
                sr = result.statement_response
                if sr and sr.result and sr.result.data_array:
                    # ``ResultManifest`` exposes columns under ``schema``
                    # (per the SDK contract); accessing ``manifest.columns``
                    # directly would raise ``AttributeError`` and was being
                    # swallowed by the surrounding ``except Exception``.
                    schema = sr.manifest.schema if sr.manifest else None
                    # ``ColumnInfo.name`` is ``str | None`` in the SDK
                    # stubs (the SDK lets you build empty descriptors);
                    # ``or ""`` keeps the join's ``Iterable[str]``
                    # contract without dropping unnamed columns silently.
                    col_names = [c.name or "" for c in (schema.columns or [])] if schema else []
                    header = " | ".join(col_names) if col_names else ""
                    rows = [" | ".join(str(v) for v in row) for row in sr.result.data_array]
                    table = "\n".join([header, "-" * len(header)] + rows) if header else "\n".join(rows)
                    parts.append(table)
            except Exception as exc:
                logger.warning("[genie] failed to fetch query result", exc_info=True)
                parts.append(f"(query result unavailable: {exc})")

    text = "\n\n".join(parts) if parts else msg.content or "(no output)"
    return text, sql


# ---------------------------------------------------------------------------
# Vector Search (backed by VectorSearchRetrieverTool from databricks_openai)
# ---------------------------------------------------------------------------

_FILTERS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Optional column filters to narrow results. "
        "Keys are column names, optionally suffixed with an operator. "
        'Supported operators: exact match {"col": "val"}, '
        'multi-match {"col": ["a","b"]}, '
        'NOT {"col NOT": "val"}, '
        '< / <= / > / >= {"col <": 100}, '
        'OR {"col1 OR col2": ["a","b"]}, '
        'LIKE {"col LIKE": "token"}. '
        "Only filter on columns whose values you know exactly."
    ),
}


def _ensure_filter_params(schema: dict[str, Any]) -> dict[str, Any]:
    """Ensure ``filters`` and ``num_results`` are exposed in the tool schema.

    The auto-generated schema from ``VectorSearchRetrieverTool`` typically
    only exposes ``query``.  We inject ``filters`` (object) and
    ``num_results`` (integer, optional override) so the LLM can use them.
    """
    props = schema.setdefault("properties", {})
    if "filters" not in props:
        props["filters"] = _FILTERS_SCHEMA
    if "num_results" not in props:
        props["num_results"] = {
            "type": "integer",
            "description": (
                "Override the default number of results to return. Useful when the user asks for more or fewer results."
            ),
        }
    return schema


def create_vector_search_tool(
    ws: WorkspaceClient,
    index_name: str,
    columns: list[str] | None = None,
    query_type: str = "HYBRID",
    num_results: int = 10,
    name: str | None = None,
    **kwargs: Any,
) -> FunctionTool:
    """Build a ``FunctionTool`` backed by ``VectorSearchRetrieverTool``.

    The ``FunctionTool`` name, description, and parameter schema are
    derived from Unity Catalog metadata automatically.  Extra *kwargs*
    are forwarded to ``VectorSearchIndex.similarity_search`` (e.g.
    ``score_threshold``).

    ``name``, when supplied, overrides the RETRIEVER span name so
    ``tool_routing_accuracy`` and friends can key off the tool's logical
    name from the skill config (e.g. ``docs_search``) rather than the
    auto-derived last segment of ``index_name``.  When omitted, falls
    back to the legacy behaviour for backward compatibility.
    """
    span_name = name or index_name.rsplit(".", 1)[-1]
    vs_tool = VectorSearchRetrieverTool(
        index_name=index_name,
        columns=columns,
        query_type=query_type,
        num_results=num_results,
        workspace_client=ws,
        **kwargs,
    )
    tool_spec = vs_tool.tool
    schema = _ensure_filter_params(tool_spec["function"]["parameters"])

    async def _invoke(_ctx: Any, args_json: str) -> str:
        args = json.loads(args_json) if args_json else {}
        query = args.get("query", "")
        if not query:
            return "Error: 'query' parameter is required."

        filters = args.get("filters")
        n_results = args.get("num_results")
        log_extra = f" filters={filters}" if filters else ""
        if n_results:
            log_extra += f" num_results={n_results}"
        logger.info("[vs] querying index %s: %s%s", index_name, query[:120], log_extra)

        execute_kwargs: dict[str, Any] = {"query": query}
        if filters:
            execute_kwargs["filters"] = [FilterItem(key=k, value=v) for k, v in filters.items()]
        if n_results:
            execute_kwargs["num_results"] = int(n_results)

        with mlflow.start_span(name=span_name, span_type="RETRIEVER") as span:
            span.set_inputs({"query": query, "filters": filters, "num_results": n_results})
            try:
                result = await asyncio.to_thread(
                    vs_tool.execute,
                    **execute_kwargs,
                )
            except Exception as exc:
                logger.warning("[vs] query failed", exc_info=True)
                span.set_outputs({"error": str(exc)})
                return f"Vector search query failed: {exc}"
            span.set_outputs(result)

        return json.dumps(result, default=str)

    return FunctionTool(
        name=tool_spec["function"]["name"],
        description=tool_spec["function"]["description"],
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )


# ---------------------------------------------------------------------------
# UC function SQL execution (used by ``data_universe``)
# ---------------------------------------------------------------------------
#
# ``uc_function`` tools are served through the MCP brick (see
# ``_tool_factory``); only the statement execution helpers remain here.


def _get_warehouse_id(ws: WorkspaceClient) -> str:
    """Return a SQL warehouse ID from env or the first available warehouse."""
    from agent_foundry import env

    wh_id = env.read_optional(env.DATABRICKS_WAREHOUSE_ID) or ""
    if wh_id:
        return wh_id
    warehouses = list(ws.warehouses.list())
    # ``warehouse.id`` is typed as ``str | None`` (the SDK lets you build
    # an in-memory ``EndpointInfo`` without an id), but warehouses
    # returned from ``list()`` always carry one.  Fail loudly if that
    # invariant ever breaks rather than returning ``None`` to a caller
    # that expects ``str``.
    for warehouse in warehouses:
        if warehouse.id:
            return warehouse.id
    raise RuntimeError("No SQL warehouse available for UC function execution")


def _is_terminal_statement(response: Any) -> bool:
    """True if the SQL statement reached a terminal lifecycle state."""
    state = getattr(getattr(response, "status", None), "state", None)
    return str(state).rsplit(".", 1)[-1] in _TERMINAL_STATEMENT_STATES


async def _execute_uc_function(
    ws: WorkspaceClient,
    *,
    sql: str,
    warehouse_id: str,
    max_wait_seconds: int,
) -> Any:
    """Execute ``sql`` synchronously, polling until terminal or budget exhausted.

    Workaround for the SDK's ~10s implicit ``wait_timeout``: we issue the
    initial call with the API max (50s) so a warming warehouse usually
    completes in one round-trip, then poll ``get_statement`` with
    exponential backoff if the statement is still PENDING/RUNNING. Caller
    is responsible for inspecting the final response state.
    """
    initial_wait = max(5, min(_UC_FUNCTION_INITIAL_WAIT_SECONDS, max_wait_seconds))
    response = await asyncio.to_thread(
        ws.statement_execution.execute_statement,
        statement=sql,
        warehouse_id=warehouse_id,
        wait_timeout=f"{initial_wait}s",
    )

    statement_id = getattr(response, "statement_id", None)
    elapsed: float = float(initial_wait)
    backoff = 2.0
    while elapsed < max_wait_seconds and statement_id and not _is_terminal_statement(response):
        await asyncio.sleep(backoff)
        elapsed += backoff
        response = await asyncio.to_thread(
            ws.statement_execution.get_statement,
            statement_id=statement_id,
        )
        backoff = min(backoff * 2, _UC_FUNCTION_POLL_BACKOFF_CAP_SECONDS)

    return response


# ---------------------------------------------------------------------------
# Python function (plain in-process callable)
# ---------------------------------------------------------------------------
#
# A ``type: python`` tool maps a config entry to a module-level Python
# callable (dotted import path) and wraps it as a ``FunctionTool`` the LLM
# can invoke directly -- no MCP / Genie / Vector Search / UC transport.
# The callable runs *in-process*; a set of such tools accumulates per-request
# state on the run ``state`` (``RequestState``) and hands the assembled object
# back through the generic ``custom_outputs`` finalize hook (see
# ``payloads.agent.runtime.agent``).
#
# ``type: python`` is an instance-author primitive: tool callables and the
# reset / finalize hooks load only from the active instance dir, enforced
# fail-closed by :func:`import_python_callable`.

# JSON Schema type for each primitive Python annotation the signature
# inferrer understands.  Anything else (containers, Pydantic models, ...)
# falls back to ``"string"``; author an explicit ``params_json_schema`` for
# richer argument shapes.
_PY_TYPE_TO_JSON: dict[type, str] = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
}

# The parameter name that opts a callable into receiving the run state: when
# a callable's *first* parameter is named ``state``, the runtime passes the
# framework's request-scoped ``RequestState`` (the SDK's ``ctx.context``) as
# the first positional argument and excludes it from the LLM-visible argument
# schema.  Any other name (including ``ctx`` / ``context``) is an ordinary
# LLM argument.
_CONTEXT_PARAM_NAMES: frozenset[str] = frozenset({"state"})

# Modules already checked for a module-level ``ContextVar`` accumulator, so
# the deprecation warning is logged once per module rather than on every load
# (hooks are loaded on every request).
_CONTEXTVAR_CHECKED: set[str] = set()


def _active_instance_root() -> Path:
    """Return the resolved active instance dir python callables must live under."""
    instance_dir = resolve_instance_dir()
    if instance_dir is None:
        raise ValueError("no active instance dir; python callables load only from instances/<name>/")
    return instance_dir.resolve()


def _is_under(location: str | None, root: Path) -> bool:
    """True if the filesystem *location* resolves inside *root*."""
    if not location:
        return False
    return Path(location).resolve().is_relative_to(root)


def _spec_is_under(spec: ModuleSpec, root: Path) -> bool:
    """True if *spec* (found, not yet executed) points inside *root*.

    A file-backed module or regular package is judged by its origin; a
    namespace package (no origin) only if every search location is inside.
    Built-in / frozen modules have no location and are never inside.
    """
    if spec.has_location:
        return _is_under(spec.origin, root)
    locations = list(spec.submodule_search_locations or [])
    return bool(locations) and all(_is_under(loc, root) for loc in locations)


def import_python_callable(path: str) -> Callable[..., Any]:
    """Import and return the callable named by a dotted *path*, fail-closed
    to the active instance dir.

    ``path`` is ``"<module>.<attr>"`` (e.g. ``"my_pkg.mod.func"``).  ``type:
    python`` is an instance-author primitive, so the callable must be
    instance code under ``instances/<name>/``:

    1. the top-level package is located *without executing it* and rejected
       unless it lives in the instance dir, so an installed module such as
       ``requests`` or ``json`` never runs, and an instance package shadowed
       by an installed one of the same name is refused;
    2. after import, the module's file must be in the instance dir;
    3. the callable's *defining* module must be too, so a re-export such as
       ``from requests import get`` inside an instance module is refused.

    Raises ``ValueError`` for a malformed path, a callable outside the
    instance dir, or a missing / non-callable attribute, and ``ImportError``
    for a module that cannot be found or imported.  Callers dispatch through
    this fail-soft (log + skip), so a rejected callable never runs and never
    crashes the agent build or the request.
    """
    module_name, _, attr = path.rpartition(".")
    if not module_name:
        raise ValueError(f"callable_path {path!r} must be 'module.attr'")
    root = _active_instance_root()

    top_level = module_name.partition(".")[0]
    spec = importlib.util.find_spec(top_level)
    if spec is None:
        raise ImportError(f"callable_path {path!r}: no module named {top_level!r}")
    if not _spec_is_under(spec, root):
        raise ValueError(
            f"callable_path {path!r} resolves outside the instance dir {root}: "
            f"{top_level!r} is not instance code (or an installed package of "
            "the same name shadows it)"
        )

    module = importlib.import_module(module_name)
    if not _is_under(getattr(module, "__file__", None), root):
        raise ValueError(f"callable_path {path!r}: module {module_name!r} is not inside the instance dir {root}")

    fn = getattr(module, attr, None)
    if not callable(fn):
        raise ValueError(f"callable_path {path!r} is not callable")
    defining = sys.modules.get(getattr(inspect.unwrap(fn), "__module__", None) or "")
    if defining is None or not _is_under(getattr(defining, "__file__", None), root):
        raise ValueError(
            f"callable_path {path!r} is defined outside the instance dir {root} "
            "(re-exported from a non-instance module?)"
        )

    _warn_if_module_contextvar(module)
    return cast(Callable[..., Any], fn)


def _warn_if_module_contextvar(module: ModuleType) -> None:
    """Log a one-time warning if *module* keeps a module-level ``ContextVar``.

    The run ``state`` is the only supported per-request accumulator; a
    module-level ``ContextVar`` only works while tools happen to run inline
    and is deprecated for this purpose.
    """
    name = module.__name__
    if name in _CONTEXTVAR_CHECKED:
        return
    _CONTEXTVAR_CHECKED.add(name)
    found = sorted(k for k, v in vars(module).items() if isinstance(v, contextvars.ContextVar))
    if found:
        logger.warning(
            "[python_fn] module %r defines module-level ContextVar(s) %s; per-request "
            "accumulation via ContextVar is deprecated and unsupported -- accumulate on "
            "the leading `state` parameter (RequestState.data) instead",
            name,
            found,
        )


def _takes_context(fn: Callable[..., Any]) -> bool:
    """True if *fn*'s first parameter opts into the run state.

    Mirrors the opt-in convention documented on
    :class:`~agent_foundry.contracts.tools.PythonFunctionTool`: a leading
    ``state`` parameter receives the request-scoped ``RequestState`` and is
    not surfaced as an LLM argument.
    """
    try:
        params = list(inspect.signature(fn).parameters.values())
    except (TypeError, ValueError):
        return False
    return bool(params) and params[0].name in _CONTEXT_PARAM_NAMES


def _infer_params_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """Infer a primitives-only JSON Schema from *fn*'s signature.

    Maps ``str`` / ``int`` / ``float`` / ``bool`` annotations to their JSON
    types (defaulting to ``"string"`` for anything else, including
    unannotated params).  A leading ``state`` parameter and any ``*args`` /
    ``**kwargs`` are skipped.  Parameters without a default are marked
    ``required``.

    This is intentionally shallow: author an explicit ``params_json_schema``
    on the tool for nested / container / Pydantic argument shapes.
    """
    properties: dict[str, Any] = {}
    required: list[str] = []
    skip_context = _takes_context(fn)

    # ``eval_str=True`` resolves string annotations to real types, which is
    # required because tool modules follow the codebase convention of
    # ``from __future__ import annotations`` (PEP 563) -- without it every
    # annotation would arrive as a ``str`` and fall through to ``"string"``.
    # Fall back to the un-evaluated signature when a forward ref cannot be
    # resolved (then unresolved annotations simply default to ``"string"``).
    try:
        params = list(inspect.signature(fn, eval_str=True).parameters.values())
    except (TypeError, ValueError, NameError):
        try:
            params = list(inspect.signature(fn).parameters.values())
        except (TypeError, ValueError):
            params = []

    for index, param in enumerate(params):
        if index == 0 and skip_context:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = param.annotation
        json_type = _PY_TYPE_TO_JSON.get(annotation, "string") if isinstance(annotation, type) else "string"
        properties[param.name] = {"type": json_type}
        if param.default is inspect.Parameter.empty:
            required.append(param.name)

    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def create_python_function_tool(
    name: str,
    description: str,
    fn: Callable[..., Any],
    params_json_schema: dict[str, Any],
) -> FunctionTool:
    """Wrap an in-process Python *fn* as a ``FunctionTool``.

    The callable is invoked directly (no external transport).  Sync
    callables run **inline** rather than via ``asyncio.to_thread``.  Per-request
    state is accumulated on the run ``state`` object (see below), which is
    threaded through the run explicitly, so state correctness does not depend
    on inline execution.  A callable that may block (SQL, network) should be
    declared ``async`` so it does not stall the event loop.

    A leading ``state`` parameter opts the callable into receiving the
    framework's request-scoped
    :class:`~payloads.agent.runtime.request_context.RequestState` -- the *bare* object
    (the SDK's ``ctx.context``), not the ``RunContextWrapper`` -- as its
    first positional argument (see :func:`_takes_context`); the parameter is
    excluded from the LLM-visible schema.  Accumulate on ``state.data``, e.g.
    ``spec = state.data.setdefault('spec', MyReportSpec())``.  A callable
    without such a parameter is called with the parsed arguments only.

    Exceptions fail **soft**: the error is logged and returned to the model
    as a short string, matching the Genie / Vector Search / UC factories.
    """
    pass_context = _takes_context(fn)
    is_async = inspect.iscoroutinefunction(fn)

    async def _invoke(ctx: Any, args_json: str) -> str:
        with mlflow.start_span(name=name, span_type="TOOL") as span:
            span.set_attribute("tool_type", "python")
            # Argument parsing is inside the try so malformed JSON (possible
            # because ``strict_json_schema=False``) fails soft to the model
            # as the docstring promises, rather than raising out of the run.
            try:
                args = json.loads(args_json) if args_json else {}
                span.set_inputs(args)
                # Pass the *bare* RequestState (the SDK's ``ctx.context``),
                # not the wrapper.  ``getattr`` degrades gracefully when a
                # test invokes ``on_invoke_tool`` with a bare state or None.
                user_state = getattr(ctx, "context", ctx)
                call_args = (user_state,) if pass_context else ()
                if is_async:
                    result = await fn(*call_args, **args)
                else:
                    result = fn(*call_args, **args)
            except Exception as exc:
                logger.warning("[python_fn] %s failed", name, exc_info=True)
                return f"Tool '{name}' failed: {exc}"
            span.set_outputs({"result_preview": str(result)[:500]})
            return result if isinstance(result, str) else json.dumps(result, default=str)

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema=params_json_schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )
