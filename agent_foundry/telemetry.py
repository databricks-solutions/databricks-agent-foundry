"""Databricks Labs User-Agent telemetry (product + adoption extras).

Labs records the SDK ``User-Agent`` on control-plane requests. This
module stamps ``product=databricks-agent-foundry`` on Foundry-owned clients and
optionally sends a once-per-process set of presence extras after deploy
(DQX-style: cheap API, swallow errors, never an invocation counter).

Privacy: extras use closed enums only for CLI cmd, payload adapter, and
LLM serving mode. MCP types, LLM provider, and model id are taken from
config/API and charset-sanitized. No instance names, queries, or user
identity.

Opt out with ``AGENT_FOUNDRY_TELEMETRY_DISABLED=true``.
"""

from __future__ import annotations

import logging
import re
from importlib import metadata as _metadata
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from agent_foundry import env
from agent_foundry.bricks import BrickResult, iter_bricks

if TYPE_CHECKING:
    from agent_foundry.public import DeployResult

logger = logging.getLogger(__name__)

PRODUCT_NAME = "databricks-agent-foundry"
# Installed wheel name in pyproject.toml; distinct from the Labs UA product token.
_DIST_NAME = "agent-foundry"
_PING_TIMEOUT_SECONDS = 5
# Databricks SDK User-Agent extra charset (partner/product extra rules).
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._+-]+$")
_INVALID_EXTRA_RE = re.compile(r"[^A-Za-z0-9._+-]+")
_MODEL_ID_MAX = 64

_ALLOWED_CMD = frozenset({"deploy", "destroy", "init", "eval"})
# Model Serving modes (Databricks FMAPI PPT / PT, external, custom).
_ALLOWED_LLM = frozenset({"fmapi", "throughput", "external", "custom", "unity_gateway", "unknown"})

_sent: set[tuple[str, str]] = set()


def reset_telemetry_cache() -> None:
    """Clear process-level extra dedup (tests)."""
    _sent.clear()


def telemetry_disabled() -> bool:
    """True when the operator opted out of Labs product attribution."""
    return env.read_bool(env.AGENT_FOUNDRY_TELEMETRY_DISABLED)


def product_version() -> str:
    """Installed distribution version, or ``0.0.0`` if unknown."""
    try:
        return _metadata.version(_DIST_NAME)
    except _metadata.PackageNotFoundError:
        return "0.0.0"


def product_kwargs() -> dict[str, str]:
    """Kwargs for ``WorkspaceClient(product=..., product_version=...)``.

    Empty when telemetry is disabled so requests are not tagged Foundry.
    """
    if telemetry_disabled():
        return {}
    return {"product": PRODUCT_NAME, "product_version": product_version()}


def _valid_token(value: str) -> bool:
    return bool(value) and _TOKEN_RE.fullmatch(value) is not None


def sanitize_extra_value(raw: str) -> str | None:
    """Map an arbitrary identifier onto the SDK extra charset.

    Invalid characters become ``-``; the result is truncated. Returns
    ``None`` when nothing legal remains. No allowlist — new model ids
    pass through as long as they sanitize.
    """
    token = _INVALID_EXTRA_RE.sub("-", raw.strip())
    token = token.strip("-_.+")
    if len(token) > _MODEL_ID_MAX:
        token = token[:_MODEL_ID_MAX].rstrip("-_.+")
    if not _valid_token(token):
        return None
    return token


def extras_from_lifecycle(
    cmd: str,
    bricks: dict[str, list[BrickResult]],
    instance_dir: Path,
    ws: Any | None = None,
) -> list[tuple[str, str]]:
    """Presence extras for one deploy or destroy (no names or counts)."""
    pairs: list[tuple[str, str]] = []
    if cmd in _ALLOWED_CMD:
        pairs.append(("cmd", cmd))
    for brick in iter_bricks():
        display = getattr(brick, "display_name", brick.name)
        results = bricks.get(display) or []
        if any(r.status == "ok" for r in results) and _valid_token(brick.name):
            pairs.append(("brick", brick.name))
    pairs.extend(_payload_extras(instance_dir))
    pairs.extend(_mcp_type_extras(instance_dir))
    pairs.extend(_llm_extras(instance_dir, ws))
    return pairs


def extras_from_deploy(
    result: DeployResult,
    instance_dir: Path,
    ws: Any | None = None,
) -> list[tuple[str, str]]:
    return extras_from_lifecycle("deploy", result.bricks, instance_dir, ws)


def _payload_extras(instance_dir: Path) -> list[tuple[str, str]]:
    try:
        from agent_foundry.deploy.payload_adapters import load_payload_config

        cfg = load_payload_config(instance_dir)
    except Exception:
        return []
    adapter = sanitize_extra_value(str(cfg.adapter).strip()) if cfg.adapter else None
    pairs: list[tuple[str, str]] = []
    if adapter:
        pairs.append(("payload", adapter))
    spec = cfg.spec if isinstance(cfg.spec, dict) else {}
    runtime = spec.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    if not runtime:
        return pairs
    tools = runtime.get("tools")
    tools = tools if isinstance(tools, dict) else {}
    if "mcp_only" in tools:
        pairs.append(("mcp_only", "true" if tools.get("mcp_only") else "false"))
    ui = spec.get("ui")
    ui = ui if isinstance(ui, dict) else {}
    chat = ui.get("chat_history")
    chat = chat if isinstance(chat, dict) else {}
    if "enabled" in chat:
        pairs.append(("lakebase", "true" if chat.get("enabled") else "false"))
    return pairs


def _llm_model_ref(instance_dir: Path) -> tuple[str, str] | None:
    """``(name, kind)`` from ``spec.runtime.model``."""
    try:
        from agent_foundry.deploy.payload_adapters import load_payload_config

        cfg = load_payload_config(instance_dir)
    except Exception:
        return None
    spec = cfg.spec if isinstance(cfg.spec, dict) else {}
    runtime = spec.get("runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    model = runtime.get("model")
    if not isinstance(model, dict):
        return None
    name, kind = model.get("name"), model.get("kind")
    if isinstance(name, str) and name.strip() and isinstance(kind, str):
        return name.strip(), kind
    return None


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _served_entities(endpoint: Any) -> list[Any]:
    config = _field(endpoint, "config")
    entities = _field(config, "served_entities") if config is not None else None
    if not entities:
        entities = _field(endpoint, "served_entities")
    if not entities:
        return []
    return list(entities)


def _is_positive(value: Any) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _provider_token(raw: Any) -> str | None:
    """Charset-sanitize the Serving API provider string; no allowlist."""
    if raw is None:
        return None
    return sanitize_extra_value(str(raw).strip().lower().replace("_", "-"))


def classify_llm_endpoint(
    name: str,
    endpoint: Any | None,
    *,
    model_kind: str = "serving_endpoint",
) -> tuple[str, str | None]:
    """Return ``(llm, llm_provider)`` from a serving-endpoint GET payload.

    A Unity Gateway model service is ``unity_gateway`` outright.  For
    serving endpoints the order is: external model → provisioned
    throughput → FMAPI pay-per-token → custom. Provider is the API
    ``external_model.provider`` when present; Databricks-hosted FM uses
    ``databricks``.
    """
    if model_kind == "model_service":
        return "unity_gateway", "databricks"
    if endpoint is not None:
        for ent in _served_entities(endpoint):
            ext = _field(ent, "external_model")
            if ext:
                return "external", _provider_token(_field(ext, "provider"))
            if (
                _is_positive(_field(ent, "min_provisioned_throughput"))
                or _is_positive(_field(ent, "max_provisioned_throughput"))
                or _is_positive(_field(ent, "provisioned_model_units"))
            ):
                return "throughput", "databricks"
            entity_name = str(_field(ent, "entity_name") or "")
            if entity_name.startswith("system.ai.") or name.startswith("databricks-"):
                return "fmapi", "databricks"
        if _served_entities(endpoint):
            return "custom", "custom"
    if name.startswith("databricks-"):
        return "fmapi", "databricks"
    return "unknown", "unknown"


def llm_model_id(name: str, endpoint: Any | None) -> str | None:
    """Model identifier from serving config, not a hardcoded catalog.

    Prefers ``external_model.name``, then the last segment of
    ``entity_name`` (Unity Catalog / ``system.ai``), then the served
    entity name, then the endpoint name from payload.yml.
    """
    if endpoint is not None:
        for ent in _served_entities(endpoint):
            ext = _field(ent, "external_model")
            if ext:
                raw = _field(ext, "name")
                if isinstance(raw, str) and raw.strip():
                    return sanitize_extra_value(raw)
            entity_name = _field(ent, "entity_name")
            if isinstance(entity_name, str) and entity_name.strip():
                leaf = entity_name.strip().rsplit(".", 1)[-1]
                return sanitize_extra_value(leaf)
            served_name = _field(ent, "name")
            if isinstance(served_name, str) and served_name.strip():
                return sanitize_extra_value(served_name)
        ep_name = _field(endpoint, "name")
        if isinstance(ep_name, str) and ep_name.strip():
            return sanitize_extra_value(ep_name)
    return sanitize_extra_value(name) if name else None


def _fetch_serving_endpoint(ws: Any, name: str) -> Any | None:
    if ws is None:
        return None
    try:
        return ws.serving_endpoints.get(name)
    except Exception as exc:
        logger.debug("serving endpoint lookup for telemetry failed: %s", exc)
        return None


def _llm_extras(instance_dir: Path, ws: Any | None) -> list[tuple[str, str]]:
    ref = _llm_model_ref(instance_dir)
    if ref is None:
        return []
    name, model_kind = ref
    endpoint = _fetch_serving_endpoint(ws, name) if model_kind == "serving_endpoint" else None
    kind, provider = classify_llm_endpoint(name, endpoint, model_kind=model_kind)
    pairs: list[tuple[str, str]] = []
    if kind in _ALLOWED_LLM:
        pairs.append(("llm", kind))
    if provider:
        pairs.append(("llm_provider", provider))
    model = llm_model_id(name, endpoint)
    if model:
        pairs.append(("llm_model", model))
    if endpoint is not None:
        pairs.append(("llm_gateway", "true" if _ai_gateway_configured(endpoint) else "false"))
    return pairs


def _ai_gateway_configured(endpoint: Any) -> bool:
    """True when the serving endpoint has an AI Gateway / Unity Gateway block."""
    gw = _field(endpoint, "ai_gateway")
    if gw is None:
        return False
    if isinstance(gw, dict) and not gw:
        return False
    return True


def _mcp_type_extras(instance_dir: Path) -> list[tuple[str, str]]:
    from agent_foundry._instance_layout import resolve_payload_subdir

    mcp_dir = resolve_payload_subdir(instance_dir, "mcp_servers")
    if not mcp_dir.is_dir():
        return []
    seen: set[str] = set()
    try:
        import yaml
    except ImportError:
        return []
    for sub in sorted(mcp_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith(("_", ".")):
            continue
        path = sub / "server.yml"
        if not path.is_file():
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if not isinstance(kind, str):
            continue
        token = sanitize_extra_value(kind.strip().lower())
        if token:
            seen.add(token)
    return [("mcp", kind) for kind in sorted(seen)]


def log_extras(ws: Any, pairs: Iterable[tuple[str, str]]) -> None:
    """Attach new extras and fire one cheap control-plane call.

    Best-effort: timeouts, missing APIs, and auth failures are swallowed.
    """
    if telemetry_disabled() or ws is None:
        return
    pending: list[tuple[str, str]] = []
    for key, value in pairs:
        if not _valid_token(key) or not _valid_token(value):
            logger.debug("dropping invalid telemetry extra %s=%s", key, value)
            continue
        item = (key, value)
        if item in _sent:
            continue
        pending.append(item)
    if not pending:
        return
    for item in pending:
        _sent.add(item)
    try:
        new_config = ws.config.copy()
        for key, value in pending:
            new_config = new_config.with_user_agent_extra(key, value)
        new_config.retry_timeout_seconds = _PING_TIMEOUT_SECONDS
        new_config.http_timeout_seconds = _PING_TIMEOUT_SECONDS
        ping = type(ws)(config=new_config)
        ping.current_user.me()
    except Exception as exc:
        logger.debug("Labs telemetry extras not sent: %s", exc)


def emit_lifecycle_telemetry(
    ws: Any,
    *,
    cmd: str,
    bricks: dict[str, list[BrickResult]],
    instance_dir: Path,
) -> None:
    """Never raises; no-op without a workspace client or when disabled."""
    try:
        log_extras(ws, extras_from_lifecycle(cmd, bricks, instance_dir, ws))
    except Exception as exc:
        logger.debug("Labs telemetry lifecycle emit failed: %s", exc)


def emit_command(cmd: str, ws: Any | None = None) -> None:
    """``cmd`` extra for ``init`` / ``eval`` (best-effort).

    Never constructs a WorkspaceClient. Pass *ws* when the caller already
    has one; otherwise this is a no-op (unit tests and offline CLI).
    """
    if cmd not in _ALLOWED_CMD or telemetry_disabled() or ws is None:
        return
    try:
        log_extras(ws, [("cmd", cmd)])
    except Exception as exc:
        logger.debug("Labs telemetry command extra not sent: %s", exc)
