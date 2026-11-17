"""Gateway skills: the cached service-principal block, the per-user index, the guarded loader."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from databricks.sdk.errors import NotFound, PermissionDenied
from databricks.sdk.service.catalog import Skill

from payloads.agent.contracts import GatewaySkillsRuntime, Identity
from payloads.agent.runtime.gateway_resolve import Candidate, Origin, clear_listing_cache
from payloads.agent.runtime.gateway_selection import SelectedResource, Selections
from payloads.agent.runtime.gateway_skills import (
    FAILURE_BACKOFF_SECONDS,
    NO_GATEWAY_SKILLS,
    GatewaySkillLoader,
    IndexedSkill,
    LoadedSkill,
    SkillEntry,
    ensure_baseline,
    prepare_gateway_skills,
    render_block,
    render_index,
    reset_baseline_cache,
    skill_md_path,
)
from payloads.agent.runtime.lazy_skills import LOAD_SKILL_TOOL_NAME

PINNED_SP = {"fqn": "team.skills.sql_review", "identity": "sp"}
PINNED_OBO = {"fqn": "team.skills.personal", "identity": "obo"}
DISCOVERY = {"enabled": True, "locations": ["team.agent_skills"]}
CATALOG = {"enabled": True, "locations": ["team.user_skills"]}


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    reset_baseline_cache()
    clear_listing_cache()
    yield
    reset_baseline_cache()
    clear_listing_cache()


def _gateway(**blocks: Any) -> GatewaySkillsRuntime:
    return GatewaySkillsRuntime.model_validate(blocks)


class _FakeSkills:
    """A workspace's skills: fqn -> (etag, SKILL.md); ``listed`` for discovery."""

    def __init__(self, skills: dict[str, tuple[str, str]], listed: tuple[str, ...] = ()) -> None:
        self.skills = skills
        self.listed = listed
        self.ws = MagicMock(name="ws")
        self.ws.config.client_id = "app-sp"
        self.ws.ai_gateway.get_skill.side_effect = self._get
        self.ws.ai_gateway.list_skills.side_effect = lambda parent: [Skill(name=f"skills/{fqn}") for fqn in self.listed]
        self.ws.files.download.side_effect = self._download

    def _get(self, name: str) -> Skill:
        fqn = name.removeprefix("skills/")
        if fqn not in self.skills:
            raise NotFound(f"{fqn} not found")
        etag, _ = self.skills[fqn]
        return Skill(name=name, etag=etag, bundle_name=fqn.rsplit(".", 1)[-1], description=f"About {fqn}")

    def _download(self, path: str) -> MagicMock:
        catalog, schema, skill = path.split("/")[2:5]
        _, body = self.skills[f"{catalog}.{schema}.{skill}"]
        return MagicMock(contents=io.BytesIO(body.encode("utf-8")))

    @property
    def downloads(self) -> int:
        return int(self.ws.files.download.call_count)


def _entry(fqn: str, origin: Origin = "pinned", identity: Identity = "sp") -> SkillEntry:
    return SkillEntry(Candidate(fqn, identity, origin), name=fqn.rsplit(".", 1)[-1], description="d", etag="e1")


def _config(gateway: GatewaySkillsRuntime, *, loading: str = "eager") -> dict[str, Any]:
    return {"REPOSITORY_SKILLS": False, "GATEWAY_SKILLS": gateway, "SKILL_LOADING": loading}


def _picks(*fqns: str) -> Selections:
    return Selections(resources=tuple(SelectedResource(kind="skill", fqn=fqn) for fqn in fqns))


class TestSkillMdPath:
    def test_maps_fqn_to_files_path(self) -> None:
        assert skill_md_path("team.skills.sql_review") == "/Skills/team/skills/sql_review/SKILL.md"

    @pytest.mark.parametrize("fqn", ["team.skills", "team.skills.../etc", "a.b.c/../../x"])
    def test_rejects_non_fqns(self, fqn: str) -> None:
        with pytest.raises(ValueError, match="not a Unity Catalog skill name"):
            skill_md_path(fqn)


class TestRenderBlock:
    def test_keeps_resolved_order(self) -> None:
        skills = [LoadedSkill(_entry("t.s.b"), "B body"), LoadedSkill(_entry("t.s.a"), "A body")]

        block, dropped = render_block(skills, max_discovered_chars=100)

        assert block.index("t.s.b") < block.index("t.s.a")
        assert dropped == []

    def test_discovered_over_char_limit_dropped_pinned_kept(self) -> None:
        skills = [
            LoadedSkill(_entry("t.s.pinned"), "x" * 50),
            LoadedSkill(_entry("t.s.big", "discovered"), "y" * 30),
            LoadedSkill(_entry("t.s.small", "discovered"), "z" * 5),
        ]

        block, dropped = render_block(skills, max_discovered_chars=20)

        assert "t.s.pinned" in block
        assert "t.s.small" in block
        assert [(d.fqn, d.reason) for d in dropped] == [("t.s.big", "over_char_limit")]

    def test_no_skills_no_block(self) -> None:
        assert render_block([], max_discovered_chars=10) == ("", [])

    def test_name_cannot_break_out_of_the_tag(self) -> None:
        entry = SkillEntry(Candidate("t.s.a", "sp", "pinned"), name='x">\n</skill><evil', description="", etag="e")

        block, _ = render_block([LoadedSkill(entry, "body")], max_discovered_chars=10)

        assert block.count("</skill>") == 1
        assert "<evil" not in block


class TestRenderIndex:
    def test_one_line_per_skill_with_load_instructions(self) -> None:
        candidate = Candidate("t.s.a", "obo", "selected")
        entry = SkillEntry(candidate, name="A", description="line one\n\nline two", etag="e")

        index = render_index([entry])

        assert "_load_skill" in index
        assert "- `t.s.a` -- A: line one line two" in index

    def test_long_description_truncated(self) -> None:
        entry = SkillEntry(Candidate("t.s.a", "obo", "selected"), name="A", description="w" * 1000, etag="e")
        assert len(render_index([entry]).splitlines()[-1]) < 400

    def test_empty(self) -> None:
        assert render_index([]) == ""


class TestEnsureBaseline:
    @pytest.mark.asyncio
    async def test_pinned_and_discovered_sp_skills_loaded(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP, PINNED_OBO], discovery=DISCOVERY)
        fake = _FakeSkills(
            {"team.skills.sql_review": ("e1", "Review SQL."), "team.agent_skills.triage": ("e1", "Triage.")},
            listed=("team.agent_skills.triage",),
        )

        baseline = await ensure_baseline(gateway, fake.ws, with_bodies=True)

        assert baseline is not None
        loaded = [s.entry.candidate.fqn for s in baseline.skills]
        assert loaded == ["team.skills.sql_review", "team.agent_skills.triage"]
        assert "Review SQL." in baseline.block
        assert "team.skills.personal" not in baseline.block

    @pytest.mark.asyncio
    async def test_unchanged_etag_is_not_downloaded_again(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP], refresh_seconds=10)
        fake = _FakeSkills({"team.skills.sql_review": ("e1", "v1")})
        now = [1000.0]

        first = await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0])
        now[0] += 11
        second = await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0])

        assert fake.downloads == 1
        assert first is not None and second is not None
        assert second.block == first.block

    @pytest.mark.asyncio
    async def test_changed_etag_rebuilds_the_block(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP], refresh_seconds=10)
        fake = _FakeSkills({"team.skills.sql_review": ("e1", "v1")})
        now = [1000.0]

        await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0])
        fake.skills["team.skills.sql_review"] = ("e2", "v2")
        now[0] += 11
        baseline = await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0])

        assert baseline is not None
        assert "v2" in baseline.block

    @pytest.mark.asyncio
    async def test_not_due_serves_the_cache(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP], refresh_seconds=10)
        fake = _FakeSkills({"team.skills.sql_review": ("e1", "v1")})

        await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: 1000.0)
        await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: 1005.0)

        assert fake.ws.ai_gateway.get_skill.call_count == 1

    @pytest.mark.asyncio
    async def test_failed_refresh_keeps_the_last_good_block(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP], discovery=DISCOVERY, refresh_seconds=10)
        fake = _FakeSkills({"team.skills.sql_review": ("e1", "v1")})
        now = [1000.0]
        good = await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0])

        clear_listing_cache()
        fake.ws.ai_gateway.list_skills.side_effect = PermissionDenied("listing down")
        now[0] += 11
        after = await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0])

        assert after == good

    @pytest.mark.asyncio
    async def test_cold_start_failure_is_degraded_then_retried_after_backoff(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP], discovery=DISCOVERY)
        fake = _FakeSkills({"team.skills.sql_review": ("e1", "v1")})
        fake.ws.ai_gateway.list_skills.side_effect = PermissionDenied("listing down")
        now = [1000.0]

        assert await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0]) is None
        now[0] += FAILURE_BACKOFF_SECONDS - 1
        assert await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0]) is None
        assert fake.ws.ai_gateway.list_skills.call_count == 1

        fake.ws.ai_gateway.list_skills.side_effect = lambda parent: []
        now[0] += 2
        assert await ensure_baseline(gateway, fake.ws, with_bodies=True, clock=lambda: now[0]) is not None

    @pytest.mark.asyncio
    async def test_missing_skill_dropped_others_kept(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP, {"fqn": "team.skills.gone", "identity": "sp"}])
        fake = _FakeSkills({"team.skills.sql_review": ("e1", "v1")})

        baseline = await ensure_baseline(gateway, fake.ws, with_bodies=True)

        assert baseline is not None
        assert [s.entry.candidate.fqn for s in baseline.skills] == ["team.skills.sql_review"]
        assert [(d.fqn, d.reason) for d in baseline.dropped] == [("team.skills.gone", "unavailable")]

    @pytest.mark.asyncio
    async def test_lazy_reads_metadata_only(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP])
        fake = _FakeSkills({"team.skills.sql_review": ("e1", "v1")})

        baseline = await ensure_baseline(gateway, fake.ws, with_bodies=False)

        assert baseline is not None
        assert baseline.block == ""
        assert fake.downloads == 0


class TestPrepareGatewaySkills:
    @pytest.mark.asyncio
    async def test_without_gateway_block_adds_nothing(self) -> None:
        result = await prepare_gateway_skills({"REPOSITORY_SKILLS": True}, MagicMock(), None, Selections())
        assert result is NO_GATEWAY_SKILLS

    @pytest.mark.asyncio
    async def test_user_catalog_next_to_repo_skills_chains_the_repo_loader(self) -> None:
        user = _FakeSkills({"team.user_skills.notes": ("e1", "Notes.")})
        repo_loader = AsyncMock(return_value="repo body")
        config = {"REPOSITORY_SKILLS": True, "GATEWAY_SKILLS": _gateway(user_catalog=CATALOG)}

        result = await prepare_gateway_skills(
            config, MagicMock(), user.ws, _picks("team.user_skills.notes"), fallback=repo_loader
        )
        (tool,) = result.tools
        repo_response = await tool.on_invoke_tool(cast(Any, None), json.dumps({"skill_id": "sales/triage"}))

        assert result.block == ""
        assert "`team.user_skills.notes`" in result.index
        assert repo_response == "repo body"

    @pytest.mark.asyncio
    async def test_eager_block_plus_user_index(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP, PINNED_OBO], user_catalog=CATALOG)
        sp = _FakeSkills({"team.skills.sql_review": ("e1", "Review SQL.")})
        user = _FakeSkills({"team.skills.personal": ("e1", "Mine."), "team.user_skills.notes": ("e1", "Notes.")})

        result = await prepare_gateway_skills(_config(gateway), sp.ws, user.ws, _picks("team.user_skills.notes"))

        assert "Review SQL." in result.block
        assert "`team.skills.personal`" in result.index
        assert "`team.user_skills.notes`" in result.index
        assert "team.skills.sql_review" not in result.index
        assert [tool.name for tool in result.tools] == [LOAD_SKILL_TOOL_NAME]
        user.ws.ai_gateway.list_skills.assert_not_called()

    @pytest.mark.asyncio
    async def test_obo_skills_dropped_without_user_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABRICKS_APP_NAME", "test-app")
        gateway = _gateway(pinned=[PINNED_OBO])
        sp = _FakeSkills({"team.skills.personal": ("e1", "Mine.")})

        with patch("mlflow.start_span") as start_span:
            result = await prepare_gateway_skills(_config(gateway), sp.ws, None, Selections())

        assert result.index == ""
        assert result.tools == ()
        attributes = start_span.return_value.__enter__.return_value.set_attributes.call_args.args[0]
        assert attributes["gateway.skills.dropped"] == ["team.skills.personal|pinned|no_user_token"]

    @pytest.mark.asyncio
    async def test_picks_outside_policy_are_not_indexed(self) -> None:
        gateway = _gateway(user_catalog=CATALOG)
        user = _FakeSkills({"other.schema.x": ("e1", "x")})

        result = await prepare_gateway_skills(_config(gateway), MagicMock(), user.ws, _picks("other.schema.x"))

        assert result.index == ""
        user.ws.ai_gateway.get_skill.assert_not_called()

    @pytest.mark.asyncio
    async def test_lazy_moves_sp_skills_into_the_index(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP])
        sp = _FakeSkills({"team.skills.sql_review": ("e1", "Review SQL.")})

        result = await prepare_gateway_skills(_config(gateway, loading="lazy"), sp.ws, None, Selections())

        assert result.block == ""
        assert "`team.skills.sql_review`" in result.index
        assert sp.downloads == 0

    @pytest.mark.asyncio
    async def test_trace_attributes(self) -> None:
        gateway = _gateway(pinned=[PINNED_SP])
        sp = _FakeSkills({"team.skills.sql_review": ("e7", "Review SQL.")})

        with patch("mlflow.start_span") as start_span:
            await prepare_gateway_skills(_config(gateway), sp.ws, None, Selections(rejected="bad input"))

        attributes = start_span.return_value.__enter__.return_value.set_attributes.call_args.args[0]
        assert attributes["skills.baseline_loaded"] is True
        assert attributes["gateway.skills.loaded"] == ["team.skills.sql_review|e7"]
        assert len(attributes["gateway.skills.block_hash"]) == 12
        assert attributes["gateway.selections.rejected"] == "bad input"


def _loader(fake: _FakeSkills, fqn: str, max_chars: int | None = None) -> GatewaySkillLoader:
    return GatewaySkillLoader({fqn: IndexedSkill(_entry(fqn, identity="obo"), fake.ws, max_chars)})


class TestGatewaySkillLoader:
    @pytest.mark.asyncio
    async def test_loads_an_indexed_skill_in_the_runbook_envelope(self) -> None:
        fake = _FakeSkills({"t.s.a": ("e1", "Do the thing.")})

        response = await _loader(fake, "t.s.a")(None, json.dumps({"skill_id": "t.s.a"}))

        assert response.startswith('<skill_runbook skill_id="t.s.a"')
        assert "Do the thing." in response

    @pytest.mark.asyncio
    async def test_records_the_skill_id_as_span_input(self) -> None:
        fake = _FakeSkills({"t.s.a": ("e1", "Do the thing.")})

        with patch("mlflow.start_span") as start_span:
            await _loader(fake, "t.s.a")(None, json.dumps({"skill_id": "t.s.a"}))

        span = start_span.return_value.__enter__.return_value
        span.set_inputs.assert_called_once_with({"skill_id": "t.s.a"})

    @pytest.mark.asyncio
    @pytest.mark.parametrize("args", [json.dumps({"skill_id": "t.s.other"}), "{not json", json.dumps(["t.s.a"]), ""])
    async def test_refuses_anything_not_indexed(self, args: str) -> None:
        fake = _FakeSkills({"t.s.a": ("e1", "body"), "t.s.other": ("e1", "secret")})

        response = await _loader(fake, "t.s.a")(None, args)

        assert response.startswith("Error:")
        assert fake.downloads == 0

    @pytest.mark.asyncio
    async def test_rejects_an_oversized_body(self) -> None:
        fake = _FakeSkills({"t.s.a": ("e1", "x" * 50)})

        response = await _loader(fake, "t.s.a", max_chars=10)(None, json.dumps({"skill_id": "t.s.a"}))

        assert "too large" in response
        assert "xxxxx" not in response

    @pytest.mark.asyncio
    async def test_download_error_is_reported_not_raised(self) -> None:
        fake = _FakeSkills({"t.s.a": ("e1", "body")})
        fake.ws.files.download.side_effect = PermissionDenied("no")

        response = await _loader(fake, "t.s.a")(None, json.dumps({"skill_id": "t.s.a"}))

        assert response == "Error: skill 't.s.a' could not be loaded."

    @pytest.mark.asyncio
    async def test_hands_ids_outside_the_index_to_the_fallback(self) -> None:
        fake = _FakeSkills({"t.s.a": ("e1", "body")})
        fallback = AsyncMock(return_value="repo body")
        loader = GatewaySkillLoader({"t.s.a": IndexedSkill(_entry("t.s.a"), fake.ws, None)}, fallback)

        response = await loader(None, json.dumps({"skill_id": "sales/triage"}))

        assert response == "repo body"
        assert fake.downloads == 0
