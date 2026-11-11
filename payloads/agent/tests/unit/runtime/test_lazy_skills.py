"""Tests for ``bricks/agent/lazy_skills.py`` (issue #98 PR 2/2).

Covers four orthogonal concerns:

1. Factory-time preload semantics -- empty/missing index handling,
   partial-deploy degradation, body content captured at factory time
   (not lazily reloaded per invocation).
2. Tool invocation happy path -- preamble formatting and body
   passthrough.
3. Tool invocation error paths -- missing ``skill_id`` arg, unknown
   ``skill_id``, both surfaced as structured strings (never raised).
4. Observability hook -- ``skill.loaded`` MLflow span attribute is set
   on every successful invocation, so the routing scorer can pick it up.

All tests run offline.  The ``_load_skill`` tool's runtime ``mlflow``
calls are patched at the module-level (``mlflow.start_span``) since the
helper imports ``mlflow`` lazily inside the closure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Coroutine, Generator
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

import pytest
from agents import FunctionTool
from agents.tool_context import ToolContext

from agent_foundry.contracts.skills import SKILL_FILE, render_skill_md
from payloads.agent.runtime.lazy_skills import (
    LOAD_SKILL_TOOL_NAME,
    SKILL_LOADED_ATTRIBUTE,
    build_load_skill_tool,
    maybe_append_load_skill_tool,
)

# ``FunctionTool.on_invoke_tool`` is typed as
# ``(ctx: ToolContext[Any], args: str) -> Awaitable[Any]`` but the
# runtime accepts ``None`` for ``ctx`` when the tool body does not
# dereference it.  These tests exercise that path uniformly; the
# single cast here keeps individual call sites terse.
_NO_CTX: ToolContext[Any] = cast("ToolContext[Any]", None)


def _run_tool(awaitable: Awaitable[Any]) -> str:
    """Run ``tool.on_invoke_tool(...)`` and return the decoded string.

    ``on_invoke_tool`` is typed ``Awaitable[Any]`` upstream but always
    returns a coroutine yielding a ``str`` at runtime.  ``asyncio.run``
    wants the narrower ``Coroutine`` type, and tests immediately use
    the result as a ``str`` -- centralising the cast keeps the call
    sites focused on their assertions.
    """
    return cast("str", asyncio.run(cast("Coroutine[Any, Any, Any]", awaitable)))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_skill(skills_root: Path, skill_id: str, *, body: str = "Body content.", name: str | None = None) -> Path:
    """Write ``<skill_id>/SKILL.md`` under ``skills_root`` and return its path; *name* defaults to *skill_id*."""
    skill_dir = skills_root / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    body_path = skill_dir / SKILL_FILE
    body_path.write_text(render_skill_md(name or skill_id, "Use when testing.", body), encoding="utf-8")
    return body_path


def _index(skill_id: str) -> dict[str, str]:
    return {skill_id: f"{skill_id}/{SKILL_FILE}"}


@pytest.fixture
def patched_mlflow_span() -> Generator[tuple[MagicMock, MagicMock], None, None]:
    """Patch ``mlflow.start_span`` and yield a mock span object.

    The mock span captures ``set_attribute`` / ``set_inputs`` /
    ``set_outputs`` calls so individual tests can assert on them.
    """
    span = MagicMock()
    cm = MagicMock()
    cm.__enter__ = MagicMock(return_value=span)
    cm.__exit__ = MagicMock(return_value=False)
    with patch("mlflow.start_span", return_value=cm) as mock_start:
        yield span, mock_start


# ---------------------------------------------------------------------------
# build_load_skill_tool: factory-time preload semantics
# ---------------------------------------------------------------------------


class TestFactoryPreload:
    """Index inspection + body preload at factory construction time."""

    def test_returns_none_when_index_empty(self, tmp_path: Path) -> None:
        assert build_load_skill_tool({}, tmp_path) is None

    def test_returns_none_when_all_bodies_missing(self, tmp_path: Path) -> None:
        # Index points to a file that never gets created.
        assert build_load_skill_tool(_index("quote-lookup"), tmp_path) is None

    def test_skips_missing_body_files_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        _write_skill(tmp_path, "quote-lookup", body="alpha")
        index = {**_index("quote-lookup"), **_index("absent-skill")}
        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.lazy_skills"):
            tool = build_load_skill_tool(index, tmp_path)
        assert tool is not None
        assert any("absent-skill" in rec.message and "missing" in rec.message for rec in caplog.records)

    def test_preloads_bodies_at_factory_time(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        body_path = _write_skill(tmp_path, "alpha", body="original body")
        tool = build_load_skill_tool(_index("alpha"), tmp_path)
        assert tool is not None
        # Mutate the on-disk body AFTER the factory ran -- the tool
        # must continue returning the preloaded text.
        _write_skill(tmp_path, "alpha", body="rewritten body")
        assert "rewritten body" in body_path.read_text(encoding="utf-8")
        out = _run_tool(tool.on_invoke_tool(_NO_CTX, '{"skill_id": "alpha"}'))
        assert "original body" in out
        assert "rewritten body" not in out


# ---------------------------------------------------------------------------
# build_load_skill_tool: tool shape
# ---------------------------------------------------------------------------


class TestToolShape:
    """Static FunctionTool surface that the SDK consumes."""

    def test_tool_name_and_required_args(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        tool = build_load_skill_tool(_index("alpha"), tmp_path)
        assert tool is not None
        assert tool.name == LOAD_SKILL_TOOL_NAME == "_load_skill"
        assert tool.params_json_schema["required"] == ["skill_id"]
        assert "skill_id" in tool.params_json_schema["properties"]


# ---------------------------------------------------------------------------
# build_load_skill_tool: invocation
# ---------------------------------------------------------------------------


class TestInvokeHappyPath:
    """Happy-path body return + preamble formatting."""

    def test_preamble_uses_frontmatter_name(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        _write_skill(tmp_path, "quote-lookup", body="STEP 1: Validate inputs.", name="quote-lookup-v2")
        tool = build_load_skill_tool(_index("quote-lookup"), tmp_path)
        assert tool is not None
        out = _run_tool(tool.on_invoke_tool(_NO_CTX, '{"skill_id": "quote-lookup"}'))
        # Response shape carries: (a) a `<skill_runbook>` opener tag
        # with the skill_id and display name as attributes (signals
        # "machine data, not chat output" to the LLM), (b) an
        # INTERNAL DIRECTIVE header so the LLM treats the body as
        # guidance, (c) the body verbatim, (d) a closing
        # `</skill_runbook>` tag, (e) a trailing bracketed postamble
        # (recency-biased one-liner -- the *last* thing the LLM reads
        # before its next action; kept tiny + bracketed so the model
        # treats it as metadata rather than echo-able content).
        assert out.startswith('<skill_runbook skill_id="quote-lookup" name="quote-lookup-v2">\n')
        assert "INTERNAL DIRECTIVE" in out
        assert "STEP 1: Validate inputs." in out
        assert "</skill_runbook>" in out
        # Postamble is the last block in the response so the trailing
        # imperative is what the LLM sees most recently.
        assert out.rstrip().endswith(
            "[next: emit one tool call to satisfy the request]",
        )

    def test_frontmatter_stays_out_of_the_runbook(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        _write_skill(tmp_path, "sql-runner", body="STEP 1.")
        tool = build_load_skill_tool(_index("sql-runner"), tmp_path)
        assert tool is not None
        out = _run_tool(tool.on_invoke_tool(_NO_CTX, '{"skill_id": "sql-runner"}'))
        assert "STEP 1." in out
        assert "description:" not in out
        assert "---\nname:" not in out

    def test_skips_unparsable_skill_md_with_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        (tmp_path / "broken").mkdir()
        (tmp_path / "broken" / SKILL_FILE).write_text("no frontmatter\n", encoding="utf-8")
        with caplog.at_level(logging.WARNING, logger="payloads.agent.runtime.lazy_skills"):
            assert build_load_skill_tool(_index("broken"), tmp_path) is None
        assert any("broken" in rec.message for rec in caplog.records)

    def test_tool_description_explicitly_warns_against_echoing(self) -> None:
        # The model picks up tools[].description in the system prompt
        # along with the response preamble, so both surfaces have to
        # carry the "internal guidance, don't echo" framing or the
        # model will paraphrase the runbook to the user before acting.
        from payloads.agent.runtime.lazy_skills import _TOOL_DESCRIPTION

        assert "internal" in _TOOL_DESCRIPTION.lower()
        assert "do not echo" in _TOOL_DESCRIPTION.lower()


class TestLegacyPromptMd:
    """Deprecated ``prompt.md`` index entries; delete with ``deploy/skills/_legacy.py``."""

    @staticmethod
    def _write_legacy_skill(skills_root: Path, skill_id: str, skill_yml: str | None) -> dict[str, str]:
        skill_dir = skills_root / skill_id
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "prompt.md").write_text("Body", encoding="utf-8")
        if skill_yml is not None:
            (skill_dir / "skill.yml").write_text(skill_yml, encoding="utf-8")
        return {skill_id: f"{skill_id}/prompt.md"}

    def test_preamble_uses_skill_yml_name(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        index = self._write_legacy_skill(tmp_path, "domain/alpha", "name: alpha_v2\n")
        tool = build_load_skill_tool(index, tmp_path)
        assert tool is not None
        out = _run_tool(tool.on_invoke_tool(_NO_CTX, '{"skill_id": "domain/alpha"}'))
        assert 'skill_id="domain/alpha" name="alpha_v2"' in out
        assert "Body" in out

    @pytest.mark.parametrize("skill_yml", [None, "name: : : oops\n"])
    def test_preamble_falls_back_to_trailing_path_component(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock], skill_yml: str | None
    ) -> None:
        index = self._write_legacy_skill(tmp_path, "domain/alpha", skill_yml)
        tool = build_load_skill_tool(index, tmp_path)
        assert tool is not None
        out = _run_tool(tool.on_invoke_tool(_NO_CTX, '{"skill_id": "domain/alpha"}'))
        assert 'skill_id="domain/alpha" name="alpha"' in out


class TestInvokeErrorPaths:
    """Structured-string error returns -- never raises into the SDK."""

    def test_missing_skill_id_arg_returns_error(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        _write_skill(tmp_path, "alpha")
        tool = build_load_skill_tool(_index("alpha"), tmp_path)
        assert tool is not None
        out = _run_tool(tool.on_invoke_tool(_NO_CTX, "{}"))
        assert out.startswith("Error:")
        assert "skill_id" in out

    def test_empty_skill_id_arg_returns_error(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        _write_skill(tmp_path, "alpha")
        tool = build_load_skill_tool(_index("alpha"), tmp_path)
        assert tool is not None
        out = _run_tool(tool.on_invoke_tool(_NO_CTX, '{"skill_id": ""}'))
        assert out.startswith("Error:")

    def test_unknown_skill_id_lists_valid_ids(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        _write_skill(tmp_path, "alpha")
        _write_skill(tmp_path, "beta")
        index = {**_index("alpha"), **_index("beta")}
        tool = build_load_skill_tool(index, tmp_path)
        assert tool is not None
        out = _run_tool(
            tool.on_invoke_tool(_NO_CTX, '{"skill_id": "missing"}'),
        )
        assert out.startswith("Error:")
        assert "'missing'" in out
        assert "'alpha'" in out
        assert "'beta'" in out


# ---------------------------------------------------------------------------
# build_load_skill_tool: MLflow span attribute (observability hook)
# ---------------------------------------------------------------------------


class TestMlflowSpanAttributes:
    """Successful invocations must set ``skill.loaded=<skill_id>``."""

    def test_sets_skill_loaded_attribute(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        span, mock_start = patched_mlflow_span
        _write_skill(tmp_path, "quote-lookup", body="Body")
        tool = build_load_skill_tool(
            _index("quote-lookup"),
            tmp_path,
        )
        assert tool is not None

        _run_tool(
            tool.on_invoke_tool(
                _NO_CTX,
                '{"skill_id": "quote-lookup"}',
            ),
        )

        mock_start.assert_called_once()
        kwargs = mock_start.call_args.kwargs
        assert kwargs["name"] == "_load_skill"
        assert kwargs["span_type"] == "TOOL"
        span.set_attribute.assert_called_with(
            SKILL_LOADED_ATTRIBUTE,
            "quote-lookup",
        )
        span.set_inputs.assert_called_once_with(
            {"skill_id": "quote-lookup"},
        )
        span.set_outputs.assert_called_once()

    def test_no_span_opened_on_error_path(
        self, tmp_path: Path, patched_mlflow_span: tuple[MagicMock, MagicMock]
    ) -> None:
        # Error paths short-circuit before the span context, so the
        # routing scorer never sees a stray skill.loaded for a failed
        # call.
        _, mock_start = patched_mlflow_span
        _write_skill(tmp_path, "alpha")
        tool = build_load_skill_tool(_index("alpha"), tmp_path)
        assert tool is not None
        _run_tool(tool.on_invoke_tool(_NO_CTX, '{"skill_id": "missing"}'))
        mock_start.assert_not_called()


# ---------------------------------------------------------------------------
# maybe_append_load_skill_tool: call-site helper
# ---------------------------------------------------------------------------


class TestMaybeAppendHelper:
    """Conditional appending used by every runtime call-site."""

    def test_eager_mode_returns_tools_unchanged(self, tmp_path: Path) -> None:
        existing: list[Any] = ["sentinel"]
        out = maybe_append_load_skill_tool(
            existing,
            {"SKILL_LOADING": "eager", "SKILL_INDEX": _index("alpha")},
            tmp_path,
        )
        assert out is existing

    def test_missing_loading_key_returns_tools_unchanged(self, tmp_path: Path) -> None:
        existing: list[Any] = ["sentinel"]
        out = maybe_append_load_skill_tool(existing, {}, tmp_path)
        assert out is existing

    def test_missing_index_returns_tools_unchanged(self, tmp_path: Path) -> None:
        existing: list[Any] = ["sentinel"]
        out = maybe_append_load_skill_tool(
            existing,
            {"SKILL_LOADING": "lazy"},
            tmp_path,
        )
        assert out is existing

    def test_empty_index_returns_tools_unchanged(self, tmp_path: Path) -> None:
        existing: list[Any] = ["sentinel"]
        out = maybe_append_load_skill_tool(
            existing,
            {"SKILL_LOADING": "lazy", "SKILL_INDEX": {}},
            tmp_path,
        )
        assert out is existing

    def test_missing_bodies_returns_tools_unchanged(self, tmp_path: Path) -> None:
        existing: list[Any] = ["sentinel"]
        # Index has an entry but no body file exists -> factory returns
        # None and the helper passes the list through.
        out = maybe_append_load_skill_tool(
            existing,
            {"SKILL_LOADING": "lazy", "SKILL_INDEX": _index("alpha")},
            tmp_path,
        )
        assert out is existing

    def test_lazy_mode_with_real_index_appends_load_skill(self, tmp_path: Path) -> None:
        _write_skill(tmp_path, "alpha")
        # ``existing`` carries a sentinel string (not a real
        # ``FunctionTool``) so we can assert pure passthrough of
        # whatever the caller already has.  ``maybe_append_load_skill_tool``
        # is typed ``list[FunctionTool]`` -- accurate for production
        # callers but overly strict for this passthrough test.  Cast at
        # the boundary AND on the result so the heterogeneous list does
        # not trip ``comparison-overlap`` on the equality check below.
        sentinel = "sentinel"
        existing: list[Any] = [sentinel]
        out = maybe_append_load_skill_tool(
            cast("list[FunctionTool]", existing),
            {"SKILL_LOADING": "lazy", "SKILL_INDEX": _index("alpha")},
            tmp_path,
        )
        assert out is not existing
        assert len(out) == 2
        # ``out[0]`` is typed ``FunctionTool`` (per the signature) but
        # at runtime is the sentinel string we passed in -- the
        # function is a pure passthrough plus append.  ``cast`` to
        # ``object`` widens enough for the identity check below
        # without lying about the actual runtime value.
        assert cast("object", out[0]) is sentinel
        assert out[1].name == LOAD_SKILL_TOOL_NAME
