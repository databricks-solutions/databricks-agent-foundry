"""Unity Catalog skills (``spec.runtime.tools.skills.unity_catalog`` and a
Unity Catalog ``user_skills`` store, read as one :class:`GatewaySkillsRuntime`).

They sit next to repository skills, which stay in the assembled system
prompt ahead of this block.

Skills reach the model in two places:

* **Baseline block** (``loading: eager``) -- the service-principal skills
  (pinned ``sp`` plus discovery) are read as the service principal and
  rendered, in resolved order, into one block placed right after the
  system prompt, inside the shared prompt-cache prefix.  It is re-checked
  every ``refresh_seconds``: only a changed etag triggers a download, and
  unchanged bodies render the same bytes, so the cache only busts on a
  real edit.  A failed refresh keeps the last good block; a failed cold
  start serves no block and retries after :data:`FAILURE_BACKOFF_SECONDS`.
* **Per-user index** -- pinned ``obo`` skills and the user's picks (and,
  with ``loading: lazy``, the service-principal skills too) are listed by
  name and description after the identity sentinel, outside the cached
  prefix.  The ``_load_skill`` tool reads a body on demand, as that
  skill's identity, and refuses any FQN that is not in the index.

Bodies are read through the Files API at
``/Skills/<catalog>/<schema>/<skill>/SKILL.md``.  The skill registry MCP is
never mounted, so its write tools stay unreachable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import mlflow
from databricks.sdk.errors.base import DatabricksError
from databricks.sdk.errors.platform import NotFound, PermissionDenied

from agent_foundry.contracts.skills import SKILL_FILE
from payloads.agent.contracts import DEFAULT_SKILL_ROUTING_HEADER, SECURABLE_FQN_PATTERN, GatewaySkillsRuntime
from payloads.agent.runtime.gateway_mcp import client_for_identity
from payloads.agent.runtime.gateway_resolve import (
    Candidate,
    Dropped,
    PinnedSource,
    Resolution,
    SpDiscoverySource,
    config_hash,
    resolve_request,
    resolve_within_limits,
)
from payloads.agent.runtime.lazy_skills import (
    LOAD_SKILL_TOOL_NAME,
    SKILL_LOADED_ATTRIBUTE,
    LoadSkillInvoke,
    load_skill_function_tool,
    wrap_runbook,
)

if TYPE_CHECKING:
    from agents import FunctionTool
    from databricks.sdk import WorkspaceClient

    from payloads.agent.runtime.gateway_selection import Selections

logger = logging.getLogger(__name__)

FAILURE_BACKOFF_SECONDS = 60.0
_MAX_INDEX_DESCRIPTION_CHARS = 300
_SECURABLE_FQN = re.compile(SECURABLE_FQN_PATTERN)
_WHITESPACE = re.compile(r"\s+")
# A missing skill or grant is permanent until someone edits UC, so that
# one skill is dropped; any other error fails the whole refresh.
_UNAVAILABLE_ERRORS = (NotFound, PermissionDenied)


@dataclass(frozen=True)
class SkillEntry:
    """A resolved skill and the metadata read for it as its identity."""

    candidate: Candidate
    name: str
    description: str
    etag: str


@dataclass(frozen=True)
class LoadedSkill:
    """A service-principal skill; ``body`` is empty in lazy mode."""

    entry: SkillEntry
    body: str = ""


@dataclass(frozen=True)
class Baseline:
    """One successful read of the service-principal skills."""

    block: str
    skills: tuple[LoadedSkill, ...]
    dropped: tuple[Dropped, ...]


SkillIndexRow = tuple[str, str, str]


@dataclass(frozen=True)
class GatewaySkills:
    """What gateway and user skills add to one request's agent; ``rows`` are ``(skill_id, name, description)``."""

    block: str = ""
    rows: tuple[SkillIndexRow, ...] = ()
    tools: tuple[FunctionTool, ...] = ()

    @property
    def index(self) -> str:
        return render_skill_index(self.rows)


NO_GATEWAY_SKILLS = GatewaySkills()


def skill_md_path(fqn: str) -> str:
    """Files API path of *fqn*'s ``SKILL.md``, refusing anything but ``catalog.schema.name``."""
    if not _SECURABLE_FQN.fullmatch(fqn):
        raise ValueError(f"not a Unity Catalog skill name: {fqn!r}")
    catalog, schema, skill = fqn.split(".")
    return f"/Skills/{catalog}/{schema}/{skill}/{SKILL_FILE}"


def fetch_entry(ws: WorkspaceClient, candidate: Candidate) -> SkillEntry:
    skill = ws.ai_gateway.get_skill(name=f"skills/{candidate.fqn}")
    return SkillEntry(
        candidate=candidate,
        name=skill.bundle_name or candidate.fqn.rsplit(".", 1)[-1],
        description=skill.description or "",
        etag=skill.etag or "",
    )


def download_skill_md(ws: WorkspaceClient, fqn: str) -> str:
    contents = ws.files.download(skill_md_path(fqn)).contents
    return contents.read().decode("utf-8") if contents is not None else ""


def _attribute(value: str) -> str:
    return _WHITESPACE.sub(" ", value).replace('"', "'").replace("<", "(").replace(">", ")").strip()


def _one_line(value: str, limit: int) -> str:
    text = _WHITESPACE.sub(" ", value).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render_block(skills: Sequence[LoadedSkill], max_discovered_chars: int) -> tuple[str, list[Dropped]]:
    """The baseline block, in the order given.

    Pinned skills always load; discovered skills share
    ``max_discovered_chars`` and one that does not fit is dropped
    (``over_char_limit``) while later, smaller ones may still fit.
    """
    sections: list[str] = []
    dropped: list[Dropped] = []
    discovered_chars = 0
    for skill in skills:
        candidate = skill.entry.candidate
        if candidate.origin == "discovered":
            if discovered_chars + len(skill.body) > max_discovered_chars:
                dropped.append(Dropped(candidate.fqn, candidate.origin, "over_char_limit"))
                continue
            discovered_chars += len(skill.body)
        sections.append(
            f'<skill name="{_attribute(skill.entry.name)}" fqn="{candidate.fqn}">\n{skill.body.strip()}\n</skill>'
        )
    if not sections:
        return "", dropped
    return "## Skills\n\n" + "\n\n".join(sections), dropped


def render_skill_index(
    rows: Sequence[SkillIndexRow],
    header: str = DEFAULT_SKILL_ROUTING_HEADER,
) -> str:
    """The ``## Loadable skills`` index of ``(skill_id, name, description)`` rows."""
    if not rows:
        return ""
    lines = [
        f"- `{skill_id}` -- {_one_line(name, 80)}: {_one_line(description, _MAX_INDEX_DESCRIPTION_CHARS)}"
        for skill_id, name, description in rows
    ]
    return "## Loadable skills\n\n" + header + "\n\n" + "\n".join(lines)


def index_rows(entries: Sequence[SkillEntry]) -> tuple[SkillIndexRow, ...]:
    return tuple((e.candidate.fqn, e.name, e.description) for e in entries)


def render_index(entries: Sequence[SkillEntry]) -> str:
    return render_skill_index(index_rows(entries))


def _read_baseline(
    gateway: GatewaySkillsRuntime,
    sp_ws: WorkspaceClient,
    previous: Baseline | None,
    *,
    with_bodies: bool,
) -> Baseline:
    """Read the service-principal skills; raises ``DatabricksError`` to keep the last good block."""
    resolution = resolve_within_limits(
        gateway,
        pinned=PinnedSource(gateway).candidates(),
        discovered=SpDiscoverySource(gateway, sp_ws).fetch(),
    )
    known = {skill.entry.candidate.fqn: skill for skill in previous.skills} if previous else {}
    skills: list[LoadedSkill] = []
    dropped = list(resolution.dropped)
    for candidate in resolution.mounted:
        if candidate.identity != "sp":
            continue
        try:
            entry = fetch_entry(sp_ws, candidate)
        except _UNAVAILABLE_ERRORS as exc:
            logger.warning("[gateway] skill %s is unavailable to the service principal: %s", candidate.fqn, exc)
            dropped.append(Dropped(candidate.fqn, candidate.origin, "unavailable"))
            continue
        cached = known.get(candidate.fqn)
        if not with_bodies:
            skills.append(LoadedSkill(entry))
        elif cached is not None and cached.entry.etag == entry.etag and cached.body:
            skills.append(LoadedSkill(entry, cached.body))
        else:
            skills.append(LoadedSkill(entry, download_skill_md(sp_ws, candidate.fqn)))
    block, over_limit = render_block(skills, gateway.discovery.limits.max_loaded_chars) if with_bodies else ("", [])
    return Baseline(block=block, skills=tuple(skills), dropped=(*dropped, *over_limit))


@dataclass
class _BaselineCache:
    """Process-lifetime cache of the baseline, for one gateway config."""

    key: tuple[str, bool] | None = None
    baseline: Baseline | None = None
    succeeded_at: float | None = None
    attempted_at: float | None = None

    def due(self, key: tuple[str, bool], refresh_seconds: int, now: float) -> bool:
        if self.key != key or self.attempted_at is None:
            return True
        if self.succeeded_at is None:
            return now - self.attempted_at >= FAILURE_BACKOFF_SECONDS
        if refresh_seconds <= 0:
            return False
        return now - self.succeeded_at >= refresh_seconds

    def current(self, key: tuple[str, bool]) -> Baseline | None:
        return self.baseline if self.key == key else None


_CACHE = _BaselineCache()
_LOCK = asyncio.Lock()


def reset_baseline_cache() -> None:
    """Drop the cached baseline.  For tests."""
    _CACHE.key = None
    _CACHE.baseline = None
    _CACHE.succeeded_at = None
    _CACHE.attempted_at = None


async def ensure_baseline(
    gateway: GatewaySkillsRuntime,
    sp_ws: WorkspaceClient,
    *,
    with_bodies: bool,
    clock: Callable[[], float] = time.monotonic,
) -> Baseline | None:
    """The cached baseline, re-read once ``refresh_seconds`` have passed.

    ``None`` means no read has succeeded yet (degraded start).  Requests do
    not queue behind a refresh when there is a block to serve.
    """
    key = (config_hash(gateway), with_bodies)
    if not _CACHE.due(key, gateway.refresh_seconds, clock()):
        return _CACHE.current(key)
    if _LOCK.locked() and _CACHE.current(key) is not None:
        return _CACHE.current(key)
    async with _LOCK:
        if not _CACHE.due(key, gateway.refresh_seconds, clock()):
            return _CACHE.current(key)
        previous = _CACHE.current(key)
        if _CACHE.key != key:
            _CACHE.key, _CACHE.baseline, _CACHE.succeeded_at = key, None, None
        _CACHE.attempted_at = clock()
        try:
            baseline = await asyncio.to_thread(_read_baseline, gateway, sp_ws, previous, with_bodies=with_bodies)
        except DatabricksError as exc:
            logger.warning("[gateway] skill refresh failed; keeping the last good skills: %s", exc)
            return previous
        _CACHE.baseline = baseline
        _CACHE.succeeded_at = clock()
        return baseline


def _max_chars(gateway: GatewaySkillsRuntime, candidate: Candidate) -> int | None:
    if candidate.origin == "discovered":
        return gateway.discovery.limits.max_loaded_chars
    if candidate.origin == "selected":
        return gateway.user_catalog.limits.max_loaded_chars
    return None


@dataclass(frozen=True)
class IndexedSkill:
    """A skill in the request's index: who reads it, and its size cap (``None`` for pinned)."""

    entry: SkillEntry
    ws: WorkspaceClient
    max_chars: int | None


class GatewaySkillLoader:
    """``_load_skill`` for gateway skills: indexed FQNs, each read as its identity; other ids go to *fallback*."""

    def __init__(self, loadables: Mapping[str, IndexedSkill], fallback: LoadSkillInvoke | None = None) -> None:
        self._loadables = dict(loadables)
        self._fallback = fallback

    async def __call__(self, ctx: Any, args_json: str) -> str:
        skill_id = parse_skill_id(args_json)
        loadable = self._loadables.get(skill_id or "")
        if loadable is None and self._fallback is not None:
            return await self._fallback(ctx, args_json)
        if loadable is None:
            valid = sorted(self._loadables)
            return f"Error: skill_id {skill_id!r} is not in the skill index. Valid skill_ids: {valid!r}."
        with mlflow.start_span(name=LOAD_SKILL_TOOL_NAME, span_type="TOOL") as span:
            span.set_inputs({"skill_id": skill_id})
            span.set_attribute(SKILL_LOADED_ATTRIBUTE, loadable.entry.candidate.fqn)
            span.set_attribute("gateway.skill.etag", loadable.entry.etag)
            return await self._load(loadable, span)

    async def _load(self, loadable: IndexedSkill, span: Any) -> str:
        fqn = loadable.entry.candidate.fqn
        try:
            body = await asyncio.to_thread(download_skill_md, loadable.ws, fqn)
        except DatabricksError as exc:
            logger.warning("[gateway] could not load skill %s: %s", fqn, exc)
            span.set_attribute("gateway.skill.rejected", "unavailable")
            return f"Error: skill {fqn!r} could not be loaded."
        if loadable.max_chars is not None and len(body) > loadable.max_chars:
            span.set_attribute("gateway.skill.rejected", "over_char_limit")
            return f"Error: skill {fqn!r} is too large to load ({len(body)} > {loadable.max_chars} characters)."
        span.set_outputs({"body_chars": len(body)})
        return wrap_runbook(fqn, loadable.entry.name, body)


def parse_skill_id(args_json: str) -> str | None:
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError:
        return None
    skill_id = args.get("skill_id") if isinstance(args, dict) else None
    return skill_id if isinstance(skill_id, str) and skill_id else None


async def _fetch_user_entries(
    gateway: GatewaySkillsRuntime,
    resolution: Resolution,
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
) -> tuple[list[IndexedSkill], list[Dropped]]:
    """Metadata for the request's ``obo`` skills, read in parallel as the user."""
    dropped: list[Dropped] = []
    pending: list[tuple[Candidate, WorkspaceClient]] = []
    for candidate in resolution.mounted:
        if candidate.identity != "obo":
            continue
        ws = client_for_identity(candidate.identity, sp_ws, user_ws)
        if ws is None:
            dropped.append(Dropped(candidate.fqn, candidate.origin, "no_user_token"))
            continue
        pending.append((candidate, ws))
    results = await asyncio.gather(
        *(asyncio.to_thread(fetch_entry, ws, candidate) for candidate, ws in pending),
        return_exceptions=True,
    )
    loadables: list[IndexedSkill] = []
    for (candidate, ws), result in zip(pending, results, strict=True):
        if isinstance(result, DatabricksError):
            logger.warning("[gateway] skill %s is unavailable to the user: %s", candidate.fqn, result)
            dropped.append(Dropped(candidate.fqn, candidate.origin, "unavailable"))
        elif isinstance(result, BaseException):
            raise result
        else:
            loadables.append(IndexedSkill(result, ws, _max_chars(gateway, candidate)))
    return loadables, dropped


def _labels(entries: Sequence[SkillEntry]) -> list[str]:
    return [f"{e.candidate.fqn}|{e.candidate.identity}|{e.candidate.origin}" for e in entries]


def _span_attributes(
    gateway: GatewaySkillsRuntime,
    baseline: Baseline | None,
    indexed: Sequence[IndexedSkill],
    dropped: Sequence[Dropped],
    selections: Selections,
) -> dict[str, Any]:
    """Names, etags, hashes and reasons only -- never skill text."""
    block = baseline.block if baseline else ""
    attributes: dict[str, Any] = {
        "gateway.config_hash": config_hash(gateway),
        "skills.baseline_loaded": baseline is not None,
        "gateway.skills.loaded": [f"{s.entry.candidate.fqn}|{s.entry.etag}" for s in baseline.skills if s.body]
        if baseline
        else [],
        "gateway.skills.block_hash": hashlib.sha256(block.encode("utf-8")).hexdigest()[:12],
        "gateway.skills.indexed": _labels([loadable.entry for loadable in indexed]),
        "gateway.skills.dropped": [f"{d.fqn}|{d.origin}|{d.reason}" for d in dropped],
    }
    if selections.rejected:
        attributes["gateway.selections.rejected"] = selections.rejected
    return attributes


async def prepare_gateway_skills(
    config: Mapping[str, Any],
    sp_ws: WorkspaceClient,
    user_ws: WorkspaceClient | None,
    selections: Selections,
    *,
    fallback: LoadSkillInvoke | None = None,
) -> GatewaySkills:
    """The block, index and ``_load_skill`` tool for one request; empty without a gateway block.

    The tool hands ids outside its index to *fallback* (the repo's lazy skills).
    """
    gateway = config.get("GATEWAY_SKILLS")
    if not isinstance(gateway, GatewaySkillsRuntime):
        return NO_GATEWAY_SKILLS
    lazy = config.get("SKILL_LOADING") == "lazy"
    with mlflow.start_span(name="gateway.skills_resolve", span_type="TASK") as span:
        baseline = await ensure_baseline(gateway, sp_ws, with_bodies=not lazy)
        resolution = await asyncio.to_thread(resolve_request, gateway, sp_ws, selections)
        user_loadables, user_dropped = await _fetch_user_entries(gateway, resolution, sp_ws, user_ws)
        sp_loadables = (
            [IndexedSkill(s.entry, sp_ws, _max_chars(gateway, s.entry.candidate)) for s in baseline.skills]
            if lazy and baseline
            else []
        )
        indexed = [*sp_loadables, *user_loadables]
        dropped = [*(baseline.dropped if baseline else ()), *_obo_drops(resolution), *user_dropped]
        span.set_attributes(_span_attributes(gateway, baseline, indexed, dropped, selections))
    loader = GatewaySkillLoader({loadable.entry.candidate.fqn: loadable for loadable in indexed}, fallback)
    return GatewaySkills(
        block="" if lazy or baseline is None else baseline.block,
        rows=index_rows([loadable.entry for loadable in indexed]),
        tools=(load_skill_function_tool(loader),) if indexed else (),
    )


def _obo_drops(resolution: Resolution) -> list[Dropped]:
    """Policy drops of the request's picks; service-principal drops come from the baseline."""
    return [d for d in resolution.dropped if d.origin == "selected"]


__all__ = [
    "FAILURE_BACKOFF_SECONDS",
    "NO_GATEWAY_SKILLS",
    "Baseline",
    "GatewaySkillLoader",
    "GatewaySkills",
    "IndexedSkill",
    "LoadedSkill",
    "SkillEntry",
    "download_skill_md",
    "ensure_baseline",
    "fetch_entry",
    "prepare_gateway_skills",
    "render_block",
    "render_index",
    "reset_baseline_cache",
    "skill_md_path",
]
