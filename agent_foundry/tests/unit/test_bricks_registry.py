"""Unit tests for the pluggable brick registry (issue #82, 68-B).

Covers:

- ``Brick`` protocol: runtime ``isinstance`` check.
- ``BrickResult`` / ``DeployContext``: schema validation.
- ``iter_bricks``: built-ins present, ordering stable.
- Entry-point extensibility: a tiny fake brick registered via a
  stubbed ``importlib.metadata.entry_points`` appears in the registry
  output (real-dist test lives in ``tests/component/test_fake_brick_entry_point.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from agent_foundry import bricks as bricks_mod
from agent_foundry.bricks import (
    Brick,
    BrickResult,
    DeployContext,
    iter_bricks,
)

# ---------------------------------------------------------------------------
# DeployContext / BrickResult schemas
# ---------------------------------------------------------------------------


class TestDeployContext:
    def test_minimal_construct(self, tmp_path: Path) -> None:
        ctx = DeployContext(instance_dir=tmp_path, target="dev")
        assert ctx.variables == {}
        assert ctx.dry_run is False
        assert ctx.ws is None
        assert ctx.config is None

    def test_all_fields(self, tmp_path: Path) -> None:
        sentinel_ws = object()
        ctx = DeployContext(
            instance_dir=tmp_path,
            target="prod",
            variables={"catalog": "main"},
            dry_run=True,
            ws=sentinel_ws,
            config={"not": "a real InstanceConfig"},
        )
        assert ctx.variables == {"catalog": "main"}
        assert ctx.dry_run is True
        assert ctx.ws is sentinel_ws

    def test_extra_keys_rejected(self, tmp_path: Path) -> None:
        # Intentional negative test: Pydantic ``extra='forbid'`` raises
        # at runtime; mypy's ``call-arg`` complaint is the static
        # mirror of the same constraint, so ignore it here.
        with pytest.raises(ValidationError):
            DeployContext(instance_dir=tmp_path, target="dev", bogus_kw=1)  # type: ignore[call-arg]


class TestBrickResult:
    def test_happy_path(self) -> None:
        r = BrickResult(name="sub1", status="ok")
        assert r.rc == 0
        assert r.details == {}

    def test_rejects_unknown_status(self) -> None:
        with pytest.raises(ValidationError):
            BrickResult(name="x", status="done")

    def test_extra_keys_rejected(self) -> None:
        # Intentional negative test (see ``TestDeployContext`` above).
        with pytest.raises(ValidationError):
            BrickResult(name="x", status="ok", unexpected="nope")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestBrickProtocol:
    def test_sql_function_satisfies_protocol(self) -> None:
        from bricks.sql_function.brick import SqlFunctionBrick

        brick = SqlFunctionBrick()
        assert isinstance(brick, Brick)
        assert brick.name == "sql_function"
        assert brick.deploy_order == 30
        assert brick.state_schema is None

    def test_ai_gateway_is_registered_before_the_app(self) -> None:
        by_name = {brick.name: brick for brick in iter_bricks()}
        assert isinstance(by_name["ai_gateway"], Brick)
        assert by_name["ai_gateway"].deploy_order < by_name["app"].deploy_order

    def test_plain_object_fails_isinstance(self) -> None:
        class NotABrick:
            pass

        assert not isinstance(NotABrick(), Brick)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestIterBricks:
    def test_contains_sql_function(self) -> None:
        names = [b.name for b in iter_bricks()]
        assert "sql_function" in names

    def test_stable_order(self) -> None:
        first = [b.name for b in iter_bricks()]
        second = [b.name for b in iter_bricks()]
        assert first == second


# ---------------------------------------------------------------------------
# Entry-point extensibility (stubbed)
# ---------------------------------------------------------------------------


class _FakeBrickState(BaseModel):
    """Minimal state schema used by the fake brick below."""

    extra: str = ""


class _FakeBrick:
    name = "fake_brick_zzz"
    deploy_order = 999
    state_schema = _FakeBrickState

    def applies_to(self, ctx: DeployContext) -> bool:  # noqa: ARG002
        return False

    def deploy(self, ctx: DeployContext) -> list[BrickResult]:  # noqa: ARG002
        return []


class _FakeEntryPoint:
    """Duck-typed ``importlib.metadata.EntryPoint`` for the stub test."""

    def __init__(self, target: Any) -> None:
        self.name = "fake_brick"
        self.value = "agent_foundry.tests:_FakeBrick"
        self._target = target

    def load(self) -> Any:
        return self._target


class TestEntryPointExtensibility:
    """Stub ``entry_points`` to exercise the third-party plugin path
    without installing a real sibling distribution.  The real-dist
    version of this test lives in ``tests/component/test_fake_brick_entry_point.py``.
    """

    def test_class_entry_point(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_eps = [_FakeEntryPoint(_FakeBrick)]
        # ``bricks_mod._metadata`` is the aliased ``importlib.metadata``
        # import (``from importlib import metadata as _metadata``);
        # patching the aliased reference mirrors what production code
        # actually calls.  Underscored re-exports trip mypy.
        monkeypatch.setattr(
            bricks_mod._metadata,  # type: ignore[attr-defined]
            "entry_points",
            lambda group=None: fake_eps if group == bricks_mod.ENTRY_POINT_GROUP else [],
        )
        names = [b.name for b in iter_bricks()]
        assert "fake_brick_zzz" in names
        # Built-ins still present.
        assert "sql_function" in names

    def test_instance_entry_point_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_eps = [_FakeEntryPoint(_FakeBrick())]
        monkeypatch.setattr(
            bricks_mod._metadata,  # type: ignore[attr-defined]  # aliased importlib.metadata
            "entry_points",
            lambda group=None: fake_eps if group == bricks_mod.ENTRY_POINT_GROUP else [],
        )
        names = [b.name for b in iter_bricks()]
        assert "fake_brick_zzz" in names

    def test_third_party_overrides_builtin_by_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A third-party brick registered with the same name as a
        built-in replaces the built-in.  This is the intended
        customisation path for downstream forks."""

        class OverrideSqlFunction(_FakeBrick):
            name = "sql_function"
            deploy_order = 30

        fake_eps = [_FakeEntryPoint(OverrideSqlFunction)]
        monkeypatch.setattr(
            bricks_mod._metadata,  # type: ignore[attr-defined]  # aliased importlib.metadata
            "entry_points",
            lambda group=None: fake_eps if group == bricks_mod.ENTRY_POINT_GROUP else [],
        )
        by_name = {b.name: b for b in iter_bricks()}
        assert isinstance(by_name["sql_function"], OverrideSqlFunction)

    def test_failed_load_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class BrokenEP:
            name = "broken"
            value = "nowhere:Thing"

            def load(self) -> Any:
                raise ImportError("kaboom")

        monkeypatch.setattr(
            bricks_mod._metadata,  # type: ignore[attr-defined]  # aliased importlib.metadata
            "entry_points",
            lambda group=None: [BrokenEP()] if group == bricks_mod.ENTRY_POINT_GROUP else [],
        )
        with pytest.raises(RuntimeError, match="Failed to load.*entry point"):
            iter_bricks()


# ---------------------------------------------------------------------------
# Deprecated private base-class name
# ---------------------------------------------------------------------------


def test_private_standard_layout_name_warns_and_aliases() -> None:
    with pytest.deprecated_call(match="StandardLayoutBrick"):
        legacy = bricks_mod._StandardLayoutBrick  # type: ignore[attr-defined]

    assert legacy is bricks_mod.StandardLayoutBrick


def test_unknown_module_attribute_still_raises() -> None:
    with pytest.raises(AttributeError, match="no_such_name"):
        _ = bricks_mod.no_such_name  # type: ignore[attr-defined]
