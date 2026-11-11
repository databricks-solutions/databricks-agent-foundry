"""Tests for the agent_app brick -- template validation, config loading,
resource building, and deploy dry-run paths.

Deploy tests require ``databricks-sdk`` and are skipped if not installed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock
from unittest.mock import MagicMock

import pytest
import yaml

BRICK_DIR = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# app.yaml template
# ---------------------------------------------------------------------------


class TestAppYamlTemplate:
    """Validate the brick-level app.yaml template."""

    def test_template_is_valid_yaml(self) -> None:
        template = BRICK_DIR / "app.yaml"
        if not template.exists():
            pytest.skip("No app.yaml template in agent_app brick")

        with open(template) as f:
            data = yaml.safe_load(f) or {}

        assert isinstance(data, dict), "app.yaml template must be a YAML mapping"

    def test_template_has_command(self) -> None:
        template = BRICK_DIR / "app.yaml"
        if not template.exists():
            pytest.skip("No app.yaml template in agent_app brick")

        with open(template) as f:
            data = yaml.safe_load(f) or {}

        assert "command" in data, "app.yaml must define a command"

    def test_template_has_env(self) -> None:
        template = BRICK_DIR / "app.yaml"
        if not template.exists():
            pytest.skip("No app.yaml template in agent_app brick")

        with open(template) as f:
            data = yaml.safe_load(f) or {}

        env_names = {e["name"] for e in data.get("env", [])}
        assert "AGENT_MODULE" in env_names, "AGENT_MODULE env var required"
        assert "INSTANCE_DIR" in env_names, "INSTANCE_DIR env var required"
        # Issue #143: pin the explicit project-root contract.
        assert "AGENT_FOUNDRY_PROJECT_ROOT" in env_names, "AGENT_FOUNDRY_PROJECT_ROOT env var required (issue #143)"


# ---------------------------------------------------------------------------
# _load_app_config
# ---------------------------------------------------------------------------

pytest.importorskip("databricks.sdk", reason="databricks-sdk not installed")

from agent_foundry.deploy.app_host import (  # noqa: E402  # importorskip gate
    _normalize_resource_dict,
    app_exists,
    build_app_resources,
    reconcile_app_spec,
    to_typed_resources,
)
from bricks.app.scripts.deploy_agent_app import deploy, main  # noqa: E402  # importorskip gate
from payloads.agent.contracts import AgentPayloadSpec  # noqa: E402
from payloads.agent.deploy.config import _load_app_config  # noqa: E402
from payloads.agent.deploy.spec import _resolve_app_payload  # noqa: E402


class TestLoadAppConfig:
    def test_returns_defaults_when_no_instance(self) -> None:
        result = _load_app_config(None, {})
        assert "app_resources" in result
        assert "sp_grants" in result
        assert "user_authorization" in result

    def test_returns_defaults_when_no_instance_config_file(self, tmp_path: Path) -> None:
        result = _load_app_config(tmp_path, {})
        assert "app_resources" in result

    def test_substitutes_variables(self, tmp_path: Path) -> None:
        # ``${var.*}`` substitution runs through the typed fields; use a
        # nested app_resources entry (the contract's flexible block).
        cfg_dir = tmp_path / "app"
        cfg_dir.mkdir()
        (cfg_dir / "config.yml").write_text(
            yaml.dump(
                {
                    "app_resources": [
                        {"name": "ep", "serving_endpoint": {"name": "prefix-${var.app_name}-suffix"}},
                    ],
                }
            )
        )

        result = _load_app_config(tmp_path, {"app_name": "my-app"})
        assert result["app_resources"][0]["serving_endpoint"]["name"] == "prefix-my-app-suffix"

    def test_preserves_nested_structure(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "app"
        cfg_dir.mkdir()
        (cfg_dir / "config.yml").write_text(
            yaml.dump(
                {
                    "app_resources": [
                        {"name": "llm", "serving_endpoint": {"name": "${var.llm_name}"}},
                    ],
                }
            )
        )

        result = _load_app_config(tmp_path, {"llm_name": "claude"})
        assert result["app_resources"][0]["serving_endpoint"]["name"] == "claude"


# ---------------------------------------------------------------------------
# _resolve_app_payload -- runtime-code seam (issue #337)
# ---------------------------------------------------------------------------


class TestResolveAppPayload:
    """The brick resolves app.yaml into a typed runtime-only payload spec."""

    def test_substitutes_vars_and_preserves_value_from(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "app"
        cfg_dir.mkdir()
        (cfg_dir / "app.yaml").write_text(
            yaml.dump(
                {
                    "command": ["python", "-m", "app"],
                    "env": [
                        {"name": "INSTANCE_DIR", "value": "instances/${var.app_name}"},
                        {"name": "MLFLOW_EXPERIMENT_ID", "valueFrom": "feedback-experiment"},
                    ],
                }
            )
        )
        payload = _resolve_app_payload(
            tmp_path,
            {"app_name": "my-app"},
            requirements_extras=("runtime",),
            spec=AgentPayloadSpec.model_validate({}),
        )
        env_by_name = {e["name"]: e for e in payload.env}
        # ``${var.*}`` substituted on the literal value...
        assert env_by_name["INSTANCE_DIR"]["value"] == "instances/my-app"
        # ...and the ``valueFrom`` entry survives untouched.
        assert env_by_name["MLFLOW_EXPERIMENT_ID"]["valueFrom"] == "feedback-experiment"
        assert payload.requirements_extras == ("runtime",)

    def test_extraction_extra_threaded_through(self, tmp_path: Path) -> None:
        payload = _resolve_app_payload(
            tmp_path,
            {},
            requirements_extras=("runtime", "extraction"),
            spec=AgentPayloadSpec.model_validate({}),
        )
        assert payload.requirements_extras == ("runtime", "extraction")

    def test_instance_dir_tracks_dir_name_not_app_name(self, tmp_path: Path) -> None:
        # The brick-default app.yaml templates INSTANCE_DIR on the instance
        # directory name (which staging copies to instances/<dir.name>), not
        # app_name. An app_name override -- e.g. for the 30-char Databricks
        # Apps limit -- must not redirect INSTANCE_DIR to a non-existent path
        # (which silently disables skills + MCP at runtime).
        instance_dir = tmp_path / "instances" / "portfolio-assistant-demo"
        instance_dir.mkdir(parents=True)
        payload = _resolve_app_payload(
            instance_dir,
            {"app_name": "portfolio-demo"},
            requirements_extras=("runtime",),
            spec=AgentPayloadSpec.model_validate({}),
        )
        env_by_name = {e["name"]: e for e in payload.env}
        assert env_by_name["INSTANCE_DIR"]["value"] == "instances/portfolio-assistant-demo"


# ---------------------------------------------------------------------------
# compute_size wiring
# ---------------------------------------------------------------------------


class TestComputeSize:
    def test_default_is_medium(self) -> None:
        result = _load_app_config(None, {})
        assert result["compute_size"] == "medium"

    def test_instance_override_large(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "app"
        cfg_dir.mkdir()
        (cfg_dir / "config.yml").write_text(yaml.dump({"compute_size": "large"}))
        result = _load_app_config(tmp_path, {})
        assert result["compute_size"] == "large"

    def test_rejects_bad_value(self, tmp_path: Path) -> None:
        from agent_foundry.loader import FoundryConfigError

        cfg_dir = tmp_path / "app"
        cfg_dir.mkdir()
        (cfg_dir / "config.yml").write_text(yaml.dump({"compute_size": "xl"}))
        with pytest.raises(FoundryConfigError, match="app/config.yml"):
            _load_app_config(tmp_path, {})

    def test_rejects_typoed_key(self, tmp_path: Path) -> None:
        from agent_foundry.loader import FoundryConfigError

        cfg_dir = tmp_path / "app"
        cfg_dir.mkdir()
        (cfg_dir / "config.yml").write_text(yaml.dump({"compute_sizee": "large"}))
        with pytest.raises(FoundryConfigError):
            _load_app_config(tmp_path, {})

    def test_create_path_sets_compute_size(self) -> None:
        """A missing app is created via the typed SDK with compute size set."""
        from databricks.sdk.service.apps import ComputeSize

        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        reconcile_app_spec(ws, "app", "desc", [], [], "large", dry_run=False)
        app_arg = ws.apps.create_and_wait.call_args.args[0]
        assert app_arg.compute_size == ComputeSize.LARGE
        ws.apps.create_update_and_wait.assert_not_called()

    def test_update_path_resizes_via_async_api(self) -> None:
        """An existing app is resized through ``create_update`` (the async
        update API) -- never the synchronous PATCH that rejects resize."""
        from databricks.sdk.service.apps import ComputeSize

        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()  # app exists
        reconcile_app_spec(ws, "app", "desc", [], [], "large", dry_run=False)
        ws.apps.create_and_wait.assert_not_called()
        kwargs = ws.apps.create_update_and_wait.call_args.kwargs
        assert "compute_size" in kwargs["update_mask"]
        assert kwargs["app"].compute_size == ComputeSize.LARGE
        ws.api_client.do.assert_not_called()


# ---------------------------------------------------------------------------
# build_app_resources
# ---------------------------------------------------------------------------


class TestBuildAppResources:
    def test_serving_endpoint(self) -> None:
        entries = [{"name": "llm", "serving_endpoint": {"name": "my-model"}}]
        result = build_app_resources(entries)
        assert len(result) == 1
        assert result[0]["name"] == "llm"
        assert result[0]["serving_endpoint"] == {"name": "my-model"}

    def test_sql_warehouse(self) -> None:
        entries = [{"name": "wh", "sql_warehouse": {"name": "starter-wh"}}]
        result = build_app_resources(entries)
        assert len(result) == 1
        assert "sql_warehouse" in result[0]

    def test_uc_securable(self) -> None:
        entries = [
            {
                "name": "vs-index",
                "uc_securable": {
                    "securable_type": "TABLE",
                    "securable_name": "cat.schema.tbl",
                    "permission": "SELECT",
                },
            }
        ]
        result = build_app_resources(entries)
        assert len(result) == 1
        assert "uc_securable" in result[0]

    def test_skips_entry_missing_name(self) -> None:
        entries = [{"serving_endpoint": {"name": "no-name"}}]
        result = build_app_resources(entries)
        assert result == []

    def test_skips_unknown_resource_type(self) -> None:
        entries = [{"name": "mystery", "unknown_type": {"foo": "bar"}}]
        result = build_app_resources(entries)
        assert result == []

    def test_includes_description(self) -> None:
        entries = [
            {
                "name": "llm",
                "serving_endpoint": {"name": "m"},
                "description": "Main LLM",
            }
        ]
        result = build_app_resources(entries)
        assert result[0]["description"] == "Main LLM"

    def test_mixed_entries(self) -> None:
        entries: list[dict[str, Any]] = [
            {"name": "llm", "serving_endpoint": {"name": "m"}},
            {"name": "wh", "sql_warehouse": {"name": "w"}},
            {"serving_endpoint": {"name": "nameless"}},
            {"name": "bad", "nonexistent": {}},
        ]
        result = build_app_resources(entries)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# typed resource conversion + genie alias normalization
# ---------------------------------------------------------------------------


class TestTypedResources:
    def test_genie_id_alias_preserved(self) -> None:
        """``genie_space.id`` (REST/config name) must map to the SDK field
        ``space_id`` -- a naive ``from_dict`` would silently drop it."""
        normalized = _normalize_resource_dict({"name": "g", "genie_space": {"id": "abc", "permission": "CAN_RUN"}})
        assert normalized["genie_space"]["space_id"] == "abc"
        assert "id" not in normalized["genie_space"]

        typed = to_typed_resources([{"name": "g", "genie_space": {"id": "abc"}}])
        genie = typed[0].genie_space
        assert genie is not None
        assert genie.space_id == "abc"

    def test_normalize_does_not_mutate_input(self) -> None:
        entry = {"name": "g", "genie_space": {"id": "abc"}}
        _normalize_resource_dict(entry)
        assert entry["genie_space"] == {"id": "abc"}

    def test_native_space_id_passed_through(self) -> None:
        typed = to_typed_resources([{"name": "g", "genie_space": {"space_id": "xyz"}}])
        genie = typed[0].genie_space
        assert genie is not None
        assert genie.space_id == "xyz"


# ---------------------------------------------------------------------------
# reconcile_app_spec -- two-phase UC securable binding
# ---------------------------------------------------------------------------


class TestReconcileAppSpec:
    def test_uc_securables_applied_in_second_phase(self) -> None:
        """Core resources go in via create; UC securables are added by a
        best-effort second ``create_update`` so a UC failure can't drop them."""
        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")  # missing -> create path
        resources = [
            {"name": "llm", "serving_endpoint": {"name": "m", "permission": "CAN_QUERY"}},
            {
                "name": "tbl",
                "uc_securable": {"securable_full_name": "c.s.t", "securable_type": "TABLE", "permission": "MODIFY"},
            },
        ]
        reconcile_app_spec(ws, "app", "desc", resources, [], "medium", dry_run=False)

        created = ws.apps.create_and_wait.call_args.args[0]
        assert [r.name for r in created.resources] == ["llm"]  # UC excluded from phase 1

        kwargs = ws.apps.create_update_and_wait.call_args.kwargs
        assert kwargs["update_mask"] == "resources"
        assert [r.name for r in kwargs["app"].resources] == ["llm", "tbl"]  # full set in phase 2

    def test_uc_failure_does_not_raise(self) -> None:
        """A phase-2 UC binding failure is downgraded to a warning."""
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()  # app exists
        ws.apps.create_update_and_wait.side_effect = [None, Exception("USE CATALOG denied")]
        resources = [
            {
                "name": "tbl",
                "uc_securable": {"securable_full_name": "c.s.t", "securable_type": "TABLE", "permission": "MODIFY"},
            },
        ]
        reconcile_app_spec(ws, "app", "desc", resources, [], "medium", dry_run=False)
        assert ws.apps.create_update_and_wait.call_count == 2  # core + UC attempted


# ---------------------------------------------------------------------------
# deploy dry-run
# ---------------------------------------------------------------------------


class TestDeployDryRun:
    """Dry-run should never call the Databricks SDK."""

    @mock.patch("bricks.app.scripts.deploy_agent_app._get_workspace_client")
    def test_dry_run_returns_zero(self, mock_ws_factory: MagicMock) -> None:
        rc = deploy(
            app_name="test-app",
            target="dev",
            variables={},
            instance_dir=None,
            dry_run=True,
        )
        assert rc == 0
        mock_ws_factory.assert_not_called()

    @mock.patch("bricks.app.scripts.deploy_agent_app._get_workspace_client")
    def test_dry_run_reads_instance_description(
        self, mock_ws_factory: MagicMock, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "foundry.toml").write_text(
            'name = "my-app"\nschema_prefix = "my_app"\ndescription = "Test application"\n'
        )

        rc = deploy(
            app_name="my-app",
            target="dev",
            variables={},
            instance_dir=tmp_path,
            dry_run=True,
        )
        assert rc == 0
        assert "Description: Test application" in capsys.readouterr().out
        mock_ws_factory.assert_not_called()


# ---------------------------------------------------------------------------
# app-exists path
# ---------------------------------------------------------------------------


class TestAppExistsHelper:
    """``app_exists`` is the gate that keeps ``ws.apps.create_and_wait``
    from running on every redeploy.  It must resolve a typed ``NotFound``
    (current SDK) *and* a bare ``Exception("RESOURCE_DOES_NOT_EXIST")``
    (legacy wrappers / older test mocks) to ``False``.
    """

    def test_typed_not_found_returns_false(self) -> None:
        from databricks.sdk.errors import NotFound

        ws = mock.MagicMock()
        ws.apps.get.side_effect = NotFound("App not found.")
        assert app_exists(ws, "missing-app") is False

    def test_legacy_substring_returns_false(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.side_effect = Exception("RESOURCE_DOES_NOT_EXIST")
        assert app_exists(ws, "missing-app") is False

    def test_existing_app_returns_true(self) -> None:
        ws = mock.MagicMock()
        ws.apps.get.return_value = mock.MagicMock()
        assert app_exists(ws, "real-app") is True


class TestDeployAppExists:
    """When the app already exists, create should be skipped."""

    # ``build_deploy_staging`` + ``subprocess`` moved into the generic
    # ``app_host`` engine (issue #337); patch them at their new home.
    @mock.patch("agent_foundry.deploy.app_host.build_deploy_staging")
    @mock.patch("agent_foundry.deploy.app_host.subprocess")
    @mock.patch("bricks.app.scripts.deploy_agent_app._get_workspace_client")
    def test_existing_app_skips_create(
        self, mock_ws_factory: MagicMock, mock_subprocess: MagicMock, mock_build_staging: MagicMock, tmp_path: Path
    ) -> None:
        mock_ws = mock.MagicMock()
        mock_ws.current_user.me.return_value = mock.MagicMock(user_name="test@co.com")
        mock_ws.apps.get.return_value = mock.MagicMock()
        mock_ws_factory.return_value = mock_ws

        mock_subprocess.run.return_value = mock.MagicMock(returncode=0, stderr="")

        mock_deployment = mock.MagicMock()
        mock_deployment.deployment_id = "dep-123"
        mock_ws.apps.deploy_and_wait.return_value = mock_deployment

        # Stub the staging helper so the test does not actually build
        # a wheel.  Returning a real TemporaryDirectory keeps the
        # ``finally: cleanup()`` path in ``_sync_source`` happy.
        import tempfile  # noqa: PLC0415

        mock_build_staging.return_value = tempfile.TemporaryDirectory()

        # The wheel-based deploy (#112) requires a real instance dir
        # to copy into the staging payload.
        instance_dir = tmp_path / "existing-app"
        instance_dir.mkdir()
        (instance_dir / "foundry.toml").write_text('name = "existing-app"\nschema_prefix = "existing_app"\n')

        rc = deploy(
            app_name="existing-app",
            target="dev",
            variables={},
            instance_dir=instance_dir,
            dry_run=False,
        )
        assert rc == 0
        mock_ws.apps.create_and_wait.assert_not_called()
        mock_ws.apps.deploy_and_wait.assert_called_once()
        mock_build_staging.assert_called_once()


# ---------------------------------------------------------------------------
# main() CLI -- regression tests for issue #45
# ---------------------------------------------------------------------------
#
# Issue #45 documented three compounding defects in the standalone CLI
# (and the regression of #29):
#
#   1. ``--instance <name>`` was joined to project_root *without* the
#      ``instances/`` prefix, silently skipping instance overrides.
#   2. ``InstanceConfig.to_variables()`` was not merged after
#      ``load_variables``, so computed fields (``app_resource_name``,
#      ``schema_prefix``, ...) were never populated.
#   3. ``--app-name`` defaulted to the literal ``"agent-bricks-app"``
#      rather than the instance's computed resource name.
#
# The tests below mock the inner ``deploy`` function and assert on the
# arguments it receives.


@pytest.fixture()
def project_with_instance(tmp_path: Path) -> Path:
    """Create a minimal project tree (matches ``test_config.project_tree``)."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "test"\n')
    (tmp_path / "foundry.toml").write_text(
        "schema_version = 2\n\n"
        "[targets.dev]\n"
        'host = "https://dev.databricks.net"\n'
        'catalog = "dev_cat"\n'
        'profile = "dev"\n\n'
        '[[instances]]\npath = "instances/my-app"\n'
    )

    inst = tmp_path / "instances" / "my-app"
    inst.mkdir(parents=True)
    (inst / "foundry.toml").write_text(
        'name = "my-app"\nschema_prefix = "my_app"\nvs_endpoint_name = "vs_docs"\nchat_ui_app_name = "my-app-ui"\n'
    )

    wh_dir = inst / "warehouse"
    wh_dir.mkdir()
    (wh_dir / "config.yml").write_text(
        yaml.dump(
            {
                "size": "Small",
                "auto_stop_mins": 5,
            }
        )
    )

    return tmp_path


class TestMainCLI:
    """Locks the three #45 regressions in the standalone main() CLI."""

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy", return_value=0)
    def test_instance_resolved_under_instances_dir(self, mock_deploy: MagicMock, project_with_instance: Path) -> None:
        """``--instance my-app`` -> ``<root>/instances/my-app`` (bug #1)."""
        rc = main(
            [
                "--instance",
                "my-app",
                "--target",
                "dev",
                "--project-root",
                str(project_with_instance),
                "--dry-run",
            ]
        )
        assert rc == 0
        kwargs = mock_deploy.call_args.kwargs
        assert kwargs["instance_dir"] == project_with_instance / "instances" / "my-app"

    def test_missing_instance_returns_error(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        """Non-existent instance directory aborts with a clear message."""
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
        rc = main(
            [
                "--instance",
                "does-not-exist",
                "--target",
                "dev",
                "--project-root",
                str(tmp_path),
            ]
        )
        assert rc == 1
        err = capsys.readouterr().err
        assert "Instance directory not found" in err
        assert "does-not-exist" in err

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy", return_value=0)
    def test_instance_config_variables_merged(self, mock_deploy: MagicMock, project_with_instance: Path) -> None:
        """``InstanceConfig.to_variables()`` is merged after load_variables (bug #2)."""
        rc = main(
            [
                "--instance",
                "my-app",
                "--target",
                "dev",
                "--project-root",
                str(project_with_instance),
                "--dry-run",
            ]
        )
        assert rc == 0
        variables = mock_deploy.call_args.args[2]
        assert variables.get("app_resource_name") == "dev-my-app-app"
        assert variables.get("schema_prefix") == "my_app"
        assert variables.get("agent_schema"), "computed agent_schema must be populated"
        assert variables.get("tracing_experiment"), "computed tracing_experiment must be populated"

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy", return_value=0)
    def test_app_name_defaults_to_app_resource_name(self, mock_deploy: MagicMock, project_with_instance: Path) -> None:
        """No ``--app-name`` -> uses computed ``app_resource_name`` (bug #3)."""
        rc = main(
            [
                "--instance",
                "my-app",
                "--target",
                "dev",
                "--project-root",
                str(project_with_instance),
                "--dry-run",
            ]
        )
        assert rc == 0
        passed_app_name = mock_deploy.call_args.args[0]
        assert passed_app_name == "dev-my-app-app"
        assert passed_app_name != "agent-bricks-app", "regression: hardcoded literal default re-introduced (#45)"

    @mock.patch("bricks.app.scripts.deploy_agent_app.deploy", return_value=0)
    def test_explicit_app_name_overrides_default(self, mock_deploy: MagicMock, project_with_instance: Path) -> None:
        """Explicit ``--app-name`` still wins over the computed default."""
        rc = main(
            [
                "--instance",
                "my-app",
                "--target",
                "dev",
                "--app-name",
                "custom-name",
                "--project-root",
                str(project_with_instance),
                "--dry-run",
            ]
        )
        assert rc == 0
        assert mock_deploy.call_args.args[0] == "custom-name"


# ---------------------------------------------------------------------------
# Wheel-based deploy staging (issue #112)
# ---------------------------------------------------------------------------
#
# The interim ``_stage_for_sync`` / ``_write_runtime_requirements``
# helpers (and their tests) were retired in #112.  The replacement --
# ``agent_foundry.deploy.staging.build_deploy_staging`` -- has its own
# coverage in ``agent_foundry/tests/unit/test_staging.py``.  This module no
# longer needs to test the staging contract directly.
