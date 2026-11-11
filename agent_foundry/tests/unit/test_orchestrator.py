"""Tests for the deployment orchestrator (scripts/deploy_all.py).

Covers instance discovery, brick deploy ordering, agent_app brick
integration, MCP config templates, and variable loading -- all
cross-cutting concerns that don't belong to a single brick.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest
import yaml
from pydantic import BaseModel

from agent_foundry.bricks import BrickResult

PROJECT_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# MCP config template
# ---------------------------------------------------------------------------


class TestMcpConfigTemplate:
    """Validate the example mcp_servers.yml template is well-formed."""

    def test_example_template_is_valid_yaml(self):
        example_path = PROJECT_ROOT / "conf" / "mcp_servers.yml.example"
        if not example_path.exists():
            pytest.skip("No mcp_servers.yml.example")

        with open(example_path) as f:
            data = yaml.safe_load(f) or {}

        if data and "mcp_servers" in data:
            assert isinstance(data["mcp_servers"], list)


# ---------------------------------------------------------------------------
# Instance discovery
# ---------------------------------------------------------------------------


class TestInstanceDiscovery:
    """deploy_all.py discovers instances regardless of which bricks they contain."""

    def test_instance_with_foundry_toml_discovered(self, tmp_path: Path):
        """Issue #295: foundry.toml is a first-class instance marker."""
        app_dir = tmp_path / "instances" / "fancy-agent"
        app_dir.mkdir(parents=True)
        (app_dir / "foundry.toml").write_text('name = "fancy-agent"\n')

        from agent_foundry.deploy._orchestrator import discover_instances

        instances = discover_instances(tmp_path / "instances")
        assert len(instances) == 1
        assert instances[0].name == "fancy-agent"

    def test_discover_ignores_legacy_marker(self, tmp_path: Path):
        """Only ``foundry.toml`` marks an instance; ``instance.yml`` alone does not."""
        (tmp_path / "instances" / "legacy").mkdir(parents=True)
        (tmp_path / "instances" / "legacy" / "instance.yml").write_text("name: legacy\n")
        (tmp_path / "instances" / "modern").mkdir(parents=True)
        (tmp_path / "instances" / "modern" / "foundry.toml").write_text('name = "modern"\n')

        from agent_foundry.deploy._orchestrator import discover_instances

        names = [d.name for d in discover_instances(tmp_path / "instances")]
        assert names == ["modern"]


# ---------------------------------------------------------------------------
# Orchestrator app deployment path
# ---------------------------------------------------------------------------


def _deploy_app_brick(instance_dir: Path, variables: dict[str, str]) -> list[BrickResult]:
    from agent_foundry import iter_bricks
    from agent_foundry.deploy._orchestrator import _dispatch_brick

    app_brick = next(b for b in iter_bricks() if b.name == "app")
    return _dispatch_brick(app_brick, instance_dir, "dev", variables, dry_run=True)


class TestOrchestratorAgentAppDeployment:
    """The orchestrator dispatches the App brick for agent instances."""

    def test_deploys_with_brick_defaults_only(self, tmp_path: Path):
        """An agent instance without app/ overrides still deploys using
        the brick-default app.yaml."""
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        (app_dir / "foundry.toml").write_text('name = "my-app"\nschema_prefix = "my_app"\n')
        # Issue #338: the App host gates on the agent payload surface
        # (``payloads/agent/``), not the retired ``agent/`` runtime dir.
        (app_dir / "payloads" / "agent").mkdir(parents=True)

        results = _deploy_app_brick(app_dir, {})
        assert len(results) == 1
        assert results[0].status == "ok"

    def test_skipped_without_agent_dir(self, tmp_path: Path):
        """Issue #337: without an agent/ runtime config the agent payload
        does not apply, leaving the App host free for other payloads."""
        app_dir = tmp_path / "my-app"
        app_dir.mkdir()
        (app_dir / "foundry.toml").write_text('name = "my-app"\nschema_prefix = "my_app"\n')

        assert _deploy_app_brick(app_dir, {}) == []

    def test_detected_when_app_yaml_exists(self, tmp_path: Path):
        app_dir = tmp_path / "my-app"
        (app_dir / "app").mkdir(parents=True)
        # Issue #338: the App host gates on the agent payload surface
        # (``payloads/agent/``), not the retired ``agent/`` runtime dir.
        (app_dir / "payloads" / "agent").mkdir(parents=True)
        (app_dir / "foundry.toml").write_text('name = "my-app"\nschema_prefix = "my_app"\n')
        (app_dir / "app" / "app.yaml").write_text(
            yaml.dump(
                {
                    "command": ["python", "bricks/app/scripts/start_app.py"],
                }
            )
        )

        with mock.patch("bricks.app.scripts.deploy_agent_app.deploy", return_value=0):
            results = _deploy_app_brick(app_dir, {"app_name": "my-app"})

        assert len(results) == 1
        assert results[0].name == "my-app"
        assert results[0].status == "ok"


# ---------------------------------------------------------------------------
# Brick deployment order
# ---------------------------------------------------------------------------
#
# Post-68-C (#84), ordering is driven by the registry (each ``Brick``
# exposes ``deploy_order``).  These tests query the registry instead
# of the removed ``BRICK_DEPLOY_ORDER`` tuple.


class TestBrickDeployOrder:
    """Verify the orchestrator deploys bricks in the correct order."""

    def test_observability_is_last(self):
        from agent_foundry import iter_bricks

        names = [b.display_name for b in iter_bricks()]
        assert names[-1] == "Observability"

    def test_all_stages(self):
        from agent_foundry import iter_bricks

        bricks = iter_bricks()
        names = [b.display_name for b in bricks]
        # Lakebase, Skills and MCP are no longer registered bricks; chat
        # history, skills assembly and MCP registration are capabilities
        # of Agent App (issue #338).
        assert len(bricks) == 11
        assert names == [
            "SQL Warehouse",
            "Data Domains",
            "SQL Functions",
            "MLflow Experiment",
            "Genie Spaces",
            "Vector Search",
            "Model Serving",
            "Jobs",
            "Unity Gateway",
            "Agent App",
            "Observability",
        ]


class TestDispatchBrickPropagatesCtxVariables:
    """Regression: mutations a brick makes to ``ctx.variables`` must reach
    the orchestrator's ``variables`` dict so subsequent bricks see upstream
    outputs (e.g. the warehouse brick publishing a freshly-created
    ``warehouse_id`` to the data-domains / sql-functions / etc. bricks).

    ``DeployContext`` is a Pydantic model since 68-B and Pydantic shallow-
    copies dict fields on construction, so without an explicit merge in
    ``_dispatch_brick`` the mutation lives only on the brick's ``ctx`` and
    is discarded when control returns.
    """

    def test_ctx_variables_mutation_reaches_caller(self, tmp_path: Path):
        from agent_foundry.bricks import BrickDestroyDefaults, BrickResult, DeployContext
        from agent_foundry.deploy._orchestrator import _dispatch_brick

        class PublishingBrick(BrickDestroyDefaults):
            name = "publisher"
            display_name = "Publisher"
            deploy_order = 1
            # ``state_schema`` is annotated on the ``Brick`` Protocol as
            # ``type[BaseModel] | None``; without an explicit annotation
            # mypy infers ``Literal[None]`` and rejects the structural
            # match.  Same pattern applied to ``AppBrick``.
            state_schema: type[BaseModel] | None = None

            def applies_to(self, ctx: DeployContext) -> bool:  # noqa: ARG002
                return True

            def deploy(self, ctx: DeployContext) -> list[BrickResult]:
                ctx.variables["warehouse_id"] = "published"
                return [BrickResult(name="x", status="ok")]

            def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:  # noqa: ARG002
                return {}

        variables = {"initial": "value"}
        _dispatch_brick(
            PublishingBrick(),
            tmp_path,
            "dev",
            variables,
            dry_run=True,
        )
        assert variables == {"initial": "value", "warehouse_id": "published"}

    def test_skipped_brick_does_not_pollute_variables(self, tmp_path: Path):
        """``applies_to`` returning False must not touch the caller's dict."""
        from agent_foundry.bricks import BrickDestroyDefaults, DeployContext
        from agent_foundry.deploy._orchestrator import _dispatch_brick

        class SkippingBrick(BrickDestroyDefaults):
            name = "skipper"
            display_name = "Skipper"
            deploy_order = 1
            state_schema: type[BaseModel] | None = None

            def applies_to(self, ctx: DeployContext) -> bool:  # noqa: ARG002
                return False

            def deploy(self, ctx: DeployContext) -> list[BrickResult]:  # noqa: ARG002
                raise AssertionError("deploy must not be called when applies_to=False")

            def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:  # noqa: ARG002
                return {}

        variables = {"initial": "value"}
        results = _dispatch_brick(
            SkippingBrick(),
            tmp_path,
            "dev",
            variables,
            dry_run=True,
        )
        assert results == []
        assert variables == {"initial": "value"}


class TestDispatchBrickWorkspaceClient:
    """Regression (#200): workspace-backed state outputs must be readable
    during the same deploy run when ``ctx.ws`` is supplied."""

    def test_load_state_outputs_uses_ws_when_provided(self, tmp_path: Path):
        from agent_foundry.bricks import BrickDestroyDefaults, BrickResult, DeployContext
        from agent_foundry.deploy._orchestrator import _dispatch_brick

        sentinel_ws = object()

        class StatePublishingBrick(BrickDestroyDefaults):
            name = "genie_like"
            display_name = "Genie Like"
            deploy_order = 50
            state_schema: type[BaseModel] | None = None

            def applies_to(self, ctx: DeployContext) -> bool:  # noqa: ARG002
                return True

            def deploy(self, ctx: DeployContext) -> list[BrickResult]:
                ctx.variables.update(self.load_state_outputs(ctx))
                return [BrickResult(name="info_schema", status="ok")]

            def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
                assert ctx.ws is sentinel_ws
                return {"genie_space_id__info_schema": "sp-123"}

        variables: dict[str, str] = {}
        _dispatch_brick(
            StatePublishingBrick(),
            tmp_path,
            "dev",
            variables,
            dry_run=False,
            ws=sentinel_ws,
        )
        assert variables["genie_space_id__info_schema"] == "sp-123"

    def test_load_state_outputs_without_ws_leaves_variable_missing(self, tmp_path: Path, monkeypatch):
        from agent_foundry.deploy._orchestrator import _dispatch_brick
        from bricks.genie.brick import GenieSpacesBrick

        genie_dir = tmp_path / "genie_spaces" / "info_schema"
        genie_dir.mkdir(parents=True)
        (genie_dir / "space.yml").write_text("name: info_schema\n")

        def _fake_deploy(sub, target, variables, action=None, dry_run=False, **_k):  # noqa: ARG001
            return 0

        payload = {"serialized_space": "{}"}

        monkeypatch.setattr("bricks.genie.scripts.deploy.deploy", _fake_deploy)
        monkeypatch.setattr("bricks.genie.scripts.assemble.assemble_api_payload", lambda *_a, **_k: payload)
        monkeypatch.setattr(
            "bricks.genie.scripts.validate.validate_serialized_space",
            lambda _ss: [],
        )
        monkeypatch.setattr(
            GenieSpacesBrick,
            "load_state_outputs",
            lambda self, ctx: {} if ctx.ws is None else {"genie_space_id__info_schema": "sp-live"},
        )

        variables: dict[str, str] = {}
        _dispatch_brick(
            GenieSpacesBrick(),
            tmp_path,
            "dev",
            variables,
            dry_run=False,
            ws=None,
        )
        assert "genie_space_id__info_schema" not in variables

        variables = {}
        _dispatch_brick(
            GenieSpacesBrick(),
            tmp_path,
            "dev",
            variables,
            dry_run=False,
            ws=object(),
        )
        assert variables["genie_space_id__info_schema"] == "sp-live"

    def test_genie_dry_run_publishes_sentinel_for_downstream_mcp(self, tmp_path: Path, monkeypatch):
        from agent_foundry.bricks import DeployContext
        from bricks.genie.brick import GenieSpacesBrick

        genie_dir = tmp_path / "genie_spaces" / "info_schema"
        genie_dir.mkdir(parents=True)
        (genie_dir / "space.yml").write_text("name: info_schema\n")

        monkeypatch.setattr("bricks.genie.scripts.deploy.deploy", lambda *_a, **_k: 0)
        monkeypatch.setattr(
            "bricks.genie.scripts.assemble.assemble_api_payload",
            lambda *_a, **_k: {"serialized_space": "{}"},
        )
        monkeypatch.setattr(
            "bricks.genie.scripts.validate.validate_serialized_space",
            lambda _ss: [],
        )

        ctx = DeployContext(instance_dir=tmp_path, target="dev", dry_run=True)
        GenieSpacesBrick().deploy(ctx)
        assert ctx.variables["genie_space_id__info_schema"] == "<DRY_RUN>"


class TestDestroyDryRunWorkspaceClient:
    """Regression: destroy --dry-run must read workspace-only state (Genie, MCP)."""

    def test_collect_destroy_plan_dry_run_includes_genie_when_ws_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_foundry.deploy._orchestrator import collect_destroy_plan

        genie_dir = tmp_path / "genie_spaces" / "info_schema"
        genie_dir.mkdir(parents=True)
        (genie_dir / "space.yml").write_text("name: info_schema\n")

        sentinel_ws = object()
        monkeypatch.setattr(
            "agent_foundry.deploy._orchestrator._try_get_workspace_client",
            lambda *_a, **_k: sentinel_ws,
        )

        def _fake_load_state(ws, _instance_name, brick_type, sub_instance, _target, **_kwargs):  # noqa: ANN001
            if ws is not sentinel_ws:
                return {}
            if brick_type == "genie_spaces" and sub_instance == "info_schema":
                return {
                    "space_id": "01f1547954181a7798fcc0b57ab219fe",
                    "title": "Test Genie",
                }
            return {}

        monkeypatch.setattr("agent_foundry.state.load_state", _fake_load_state)
        monkeypatch.setattr("agent_foundry.destroy_base.load_state", _fake_load_state)

        plan = collect_destroy_plan(tmp_path, "dev", dry_run=True)
        genie_items = [item for item in plan.items if item.brick == "genie_spaces"]
        assert len(genie_items) == 1
        assert "01f1547954181a7798fcc0b57ab219fe" in genie_items[0].resource

    def test_collect_destroy_plan_dry_run_skips_genie_without_ws(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_foundry.deploy._orchestrator import collect_destroy_plan

        genie_dir = tmp_path / "genie_spaces" / "info_schema"
        genie_dir.mkdir(parents=True)
        (genie_dir / "space.yml").write_text("name: info_schema\n")

        monkeypatch.setattr(
            "agent_foundry.deploy._orchestrator._try_get_workspace_client",
            lambda *_a, **_k: None,
        )

        plan = collect_destroy_plan(tmp_path, "dev", dry_run=True)
        genie_items = [item for item in plan.items if item.brick == "genie_spaces"]
        assert genie_items == []

    def test_destroy_instance_dry_run_invokes_genie_destroy_preview(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from agent_foundry.deploy._orchestrator import destroy_instance

        genie_dir = tmp_path / "genie_spaces" / "info_schema"
        genie_dir.mkdir(parents=True)
        (genie_dir / "space.yml").write_text("name: info_schema\n")

        sentinel_ws = object()
        monkeypatch.setattr(
            "agent_foundry.deploy._orchestrator._try_get_workspace_client",
            lambda *_a, **_k: sentinel_ws,
        )

        def _fake_load_state(ws, _instance_name, brick_type, sub_instance, _target, **_kwargs):  # noqa: ANN001
            if ws is not sentinel_ws:
                return {}
            if brick_type == "genie_spaces" and sub_instance == "info_schema":
                return {
                    "space_id": "01f1547954181a7798fcc0b57ab219fe",
                    "title": "Test Genie",
                }
            return {}

        monkeypatch.setattr("agent_foundry.state.load_state", _fake_load_state)
        monkeypatch.setattr("agent_foundry.destroy_base.load_state", _fake_load_state)

        destroy_calls: list[tuple[str | None, bool]] = []

        def _fake_deploy(sub, target, variables, action=None, dry_run=False, **_k):  # noqa: ANN001, ARG001
            destroy_calls.append((action, dry_run))
            return 0

        monkeypatch.setattr("bricks.genie.scripts.deploy.deploy", _fake_deploy)

        result = destroy_instance(
            tmp_path,
            "dev",
            variables={},
            dry_run=True,
            skip_bricks={
                "observability",
                "agent app",
                "agent_app",
                "mcp",
                "skills",
                "vector",
                "mlflow",
                "sql function",
                "data",
                "warehouse",
            },
        )

        assert ("destroy", True) in destroy_calls
        genie_results = result.bricks.get("Genie Spaces", [])
        assert genie_results
        assert genie_results[0].status == "ok"

    def test_genie_deploy_destroy_dry_run_reads_workspace_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from bricks.genie.scripts.deploy import deploy

        genie_dir = tmp_path / "genie_spaces" / "info_schema"
        genie_dir.mkdir(parents=True)

        sentinel_ws = object()
        monkeypatch.setattr("bricks.genie.scripts.deploy._get_client", lambda *_a, **_k: sentinel_ws)
        monkeypatch.setattr(
            "bricks.genie.scripts.deploy._load_state",
            lambda ws, *_a, **_k: (
                {"space_id": "01f1547954181a7798fcc0b57ab219fe", "title": "Test Genie"} if ws is sentinel_ws else {}
            ),
        )

        rc = deploy(genie_dir, "dev", {}, action="destroy", dry_run=True)
        assert rc == 0
        assert "Would destroy space 01f1547954181a7798fcc0b57ab219fe" in capsys.readouterr().out

    def test_offline_never_constructs_workspace_client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_foundry.deploy._orchestrator import (
            collect_destroy_plan,
            deploy_instance,
            destroy_instance,
        )

        constructed: list[object] = []

        def _boom(*_a: object, **_k: object) -> None:
            constructed.append(1)
            raise AssertionError("factory must not run when offline")

        monkeypatch.setattr("agent_foundry.workspace_client.get_workspace_client", _boom)
        monkeypatch.setattr(
            "agent_foundry.payload_requirements.enforce_payload_requirements",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr("agent_foundry.deploy._orchestrator.iter_bricks", lambda: [])

        deploy_instance(tmp_path, "dev", variables={"app_name": "x"}, dry_run=True, offline=True)
        destroy_instance(tmp_path, "dev", variables={"app_name": "x"}, dry_run=True, offline=True)
        collect_destroy_plan(tmp_path, "dev", variables={"app_name": "x"}, dry_run=True, offline=True)
        assert constructed == []

    def test_offline_does_not_set_skip_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_foundry.deploy._orchestrator import _try_get_workspace_client
        from agent_foundry.workspace_client import SKIP_DEFAULT_WORKSPACE_CLIENT_ENV

        monkeypatch.delenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, raising=False)
        constructed: list[object] = []

        def _boom(*_a: object, **_k: object) -> None:
            constructed.append(1)
            raise AssertionError("factory must not run when offline")

        monkeypatch.setattr("agent_foundry.workspace_client.get_workspace_client", _boom)
        assert _try_get_workspace_client({}, offline=True) is None
        assert constructed == []
        assert SKIP_DEFAULT_WORKSPACE_CLIENT_ENV not in os.environ

    def test_offline_without_skip_env_does_not_construct(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_foundry.deploy._orchestrator import deploy_instance
        from agent_foundry.workspace_client import SKIP_DEFAULT_WORKSPACE_CLIENT_ENV

        monkeypatch.delenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, raising=False)
        constructed: list[object] = []

        def _boom(*_a: object, **_k: object) -> None:
            constructed.append(1)
            raise AssertionError("factory must not run when offline")

        monkeypatch.setattr("agent_foundry.workspace_client.get_workspace_client", _boom)
        monkeypatch.setattr(
            "agent_foundry.payload_requirements.enforce_payload_requirements",
            lambda *_a, **_k: None,
        )
        monkeypatch.setattr("agent_foundry.deploy._orchestrator.iter_bricks", lambda: [])
        deploy_instance(tmp_path, "dev", variables={"app_name": "x"}, dry_run=True, offline=True)
        assert constructed == []


class TestResolveDeployerUsesInjectedClient:
    def test_live_uses_injected_ws_not_factory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_foundry.deploy._orchestrator import resolve_deployer

        constructed: list[object] = []

        def _boom(*_a: object, **_k: object) -> None:
            constructed.append(1)
            raise AssertionError("resolve_deployer must not call the factory")

        monkeypatch.setattr("agent_foundry.workspace_client.get_workspace_client", _boom)

        class _Me:
            user_name = "injected@example.com"

        class _User:
            def me(self) -> _Me:
                return _Me()

        class _Ws:
            current_user = _User()

        variables = resolve_deployer({}, dry_run=False, ws=_Ws())
        assert variables["deployer"] == "injected@example.com"
        assert constructed == []

    def test_dry_run_uses_placeholder_without_ws(self) -> None:
        from agent_foundry.deploy._orchestrator import resolve_deployer

        variables = resolve_deployer({}, dry_run=True)
        assert variables["deployer"] == "dry_run_user"


# ---------------------------------------------------------------------------
# State-schema contract enforcement (issue #156)
# ---------------------------------------------------------------------------
#
# When a brick declares a ``state_schema`` and its on-disk state file
# violates the contract, the orchestrator's ``_preload_state`` must
# re-raise instead of swallowing the error as a plain warning -- a
# downstream brick consuming invalid state would crash with a delayed
# KeyError otherwise.


class TestStateSchemaPropagation:
    def test_preload_state_propagates_validation_error(self, tmp_path: Path):
        from agent_foundry import BrickStateValidationError
        from agent_foundry.deploy._orchestrator import _preload_state
        from agent_foundry.state import save_state
        from agent_foundry.testing.fakes import FakeWorkspaceClient

        # Warehouse declares state_schema = WarehouseState (#156 pilot).
        # State missing the required ``name`` key must surface a clear
        # error to the caller.
        (tmp_path / "warehouse").mkdir()
        ws = FakeWorkspaceClient()
        save_state(ws, tmp_path.name, "warehouse", tmp_path.name, "dev", {"warehouse_id": "wh-only"})

        with pytest.raises(BrickStateValidationError) as exc:
            _preload_state(tmp_path, "dev", {}, dry_run=True, ws=ws)
        assert "warehouse" in str(exc.value)

    def test_preload_state_swallows_other_errors_as_warning(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ):
        # Non-validation errors are permissive: warn and continue so a
        # transient I/O blip does not block a deploy.  Validation errors
        # are the only halt-the-world case.
        from agent_foundry import state
        from agent_foundry.deploy._orchestrator import _preload_state
        from agent_foundry.testing.fakes import FakeWorkspaceClient

        def _raise_io_error(*_args: object) -> dict[str, str]:
            raise OSError("workspace unreachable")

        (tmp_path / "warehouse").mkdir()
        monkeypatch.setattr(state, "load_state", _raise_io_error)

        # Must not raise.  Behaviour matches pre-#156.
        _preload_state(tmp_path, "dev", {}, dry_run=True, ws=FakeWorkspaceClient())
        captured = capsys.readouterr()
        assert "WARNING" in captured.out
        assert "warehouse" in captured.out


class _FakeBrick:
    """Minimal stand-in for a registered brick used by ``only_bricks`` tests."""

    def __init__(self, name: str, display_name: str, deploy_order: int) -> None:
        self.name = name
        self.display_name = display_name
        self.deploy_order = deploy_order


class TestDeployInstanceOnlyBricks:
    """``only_bricks`` allowlist filtering in :func:`deploy_instance`."""

    def test_only_bricks_deploys_matching_and_skips_rest(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from agent_foundry.bricks import BrickResult
        from agent_foundry.deploy import _orchestrator

        bricks = [
            _FakeBrick("warehouse", "SQL Warehouse", 10),
            _FakeBrick("skills", "Skills", 70),
            _FakeBrick("app", "Agent App", 90),
        ]
        monkeypatch.setattr(_orchestrator, "iter_bricks", lambda: bricks)
        monkeypatch.setattr(_orchestrator, "_preload_state", lambda *a, **k: None)
        monkeypatch.setattr(_orchestrator, "_try_get_workspace_client", lambda *_a, **_k: None)

        dispatched: list[str] = []

        def _fake_dispatch(brick, *_a, **_k):  # noqa: ANN001, ANN202
            dispatched.append(brick.name)
            return [BrickResult(name="*", status="ok", rc=0)]

        monkeypatch.setattr(_orchestrator, "_dispatch_brick", _fake_dispatch)

        result = _orchestrator.deploy_instance(
            tmp_path,
            "dev",
            variables={},
            dry_run=True,
            only_bricks={"warehouse", "skills"},
        )

        assert dispatched == ["warehouse", "skills"]
        assert result.bricks["Agent App"][0].status == "skipped"
        assert result.bricks["SQL Warehouse"][0].status == "ok"
        assert result.bricks["Skills"][0].status == "ok"
