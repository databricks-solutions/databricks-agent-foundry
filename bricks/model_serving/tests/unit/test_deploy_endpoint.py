"""Offline tests for the Model Serving brick (fake workspace, no network)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from databricks.sdk.errors import NotFound

from agent_foundry import state as shared_state
from agent_foundry.bricks import DeployContext, DestroyContext
from agent_foundry.contracts.model_serving import ServingEndpointConfig
from agent_foundry.loader import FoundryConfigError
from agent_foundry.testing.fakes import FakeWorkspaceClient
from bricks.model_serving.brick import ModelServingBrick
from bricks.model_serving.scripts import deploy_endpoint as de

MODEL = "cat.sch.churn"
SCAFFOLD = Path(__file__).resolve().parents[2] / "_scaffold" / "endpoint_config.yml"
VARIABLES = {"app_name": "demo-app", "catalog": "cat", "schema": "sch", "deployer": "me"}


class _Endpoints:
    def __init__(self) -> None:
        self.endpoints: dict[str, Any] = {}
        self.created: list[dict[str, Any]] = []
        self.updated: list[Any] = []
        self.deleted: list[str] = []
        self.fail_create = False

    def get(self, name: str) -> Any:
        if name not in self.endpoints:
            raise NotFound(f"endpoint {name} does not exist")
        return self.endpoints[name]

    def create_and_wait(self, *, name: str, config: Any, tags: list[Any], timeout: Any) -> Any:
        del timeout
        if self.fail_create:
            raise RuntimeError("container build failed")
        self.created.append({"name": name, "config": config, "tags": {tag.key: tag.value for tag in tags}})
        self.endpoints[name] = SimpleNamespace(config=SimpleNamespace(served_entities=config.served_entities))
        return self.endpoints[name]

    def wait_get_serving_endpoint_not_updating(self, name: str, *, timeout: Any) -> Any:
        del timeout
        return self.endpoints[name]

    def update_config_and_wait(self, *, name: str, served_entities: list[Any], timeout: Any) -> Any:
        del timeout
        self.updated.append(served_entities[0])
        self.endpoints[name] = SimpleNamespace(config=SimpleNamespace(served_entities=served_entities))
        return self.endpoints[name]

    def delete(self, name: str) -> None:
        self.deleted.append(name)
        self.endpoints.pop(name, None)

    def build_logs(self, name: str, served_model_name: str) -> Any:
        del name, served_model_name
        return SimpleNamespace(logs="pip install ...\nImportError: libcudnn")


class _Registry:
    """``model_versions`` + ``registered_models`` over one dict of model -> versions."""

    def __init__(self) -> None:
        self.versions: dict[str, list[int]] = {}
        self.aliases: dict[tuple[str, str], int] = {}
        self.deleted: list[str] = []

    def list(self, full_name: str) -> list[Any]:
        return [SimpleNamespace(version=version) for version in self.versions.get(full_name, [])]

    def get(self, *, full_name: str, version: int | None = None) -> Any:
        known = self.versions.get(full_name)
        if known is None or (version is not None and version not in known):
            raise NotFound(f"{full_name} v{version} does not exist")
        return SimpleNamespace(full_name=full_name, version=version)

    def get_by_alias(self, full_name: str, alias: str) -> Any:
        return SimpleNamespace(version=self.aliases[(full_name, alias)])

    def delete(self, full_name: str) -> None:
        self.deleted.append(full_name)
        self.versions.pop(full_name, None)


class _Jobs:
    """``jobs.submit`` that registers a new model version, like a real build script."""

    def __init__(self, registry: _Registry, *, registers: bool = True) -> None:
        self.registry = registry
        self.registers = registers
        self.submitted: list[dict[str, Any]] = []

    def submit(self, **kwargs: Any) -> Any:
        self.submitted.append(kwargs)
        if self.registers:
            versions = self.registry.versions.setdefault(MODEL, [])
            versions.append(max(versions, default=0) + 1)
        return SimpleNamespace(result=lambda timeout: None)


class _Experiments:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def get_by_name(self, *, experiment_name: str) -> Any:
        del experiment_name
        return SimpleNamespace(experiment=SimpleNamespace(experiment_id="exp-1"))

    def delete_experiment(self, experiment_id: str) -> None:
        self.deleted.append(experiment_id)


class _ServingClient(FakeWorkspaceClient):
    def __init__(self, *, build_registers: bool = True) -> None:
        super().__init__()
        self.serving_endpoints = _Endpoints()
        self.model_versions = _Registry()
        self.registered_models = self.model_versions
        self.jobs = _Jobs(self.model_versions, registers=build_registers)
        self.experiments = _Experiments()


def _sub_instance(tmp_path: Path, config: dict[str, Any], files: dict[str, str] | None = None) -> Path:
    sub = tmp_path / "instances" / "demo" / "model_serving" / "churn"
    sub.mkdir(parents=True)
    (sub / "endpoint_config.yml").write_text(yaml.safe_dump(config), encoding="utf-8")
    for name, body in (files or {}).items():
        (sub / name).write_text(body, encoding="utf-8")
    return sub


def _config(**model: Any) -> dict[str, Any]:
    return {"endpoint_name": "demo-churn", "model": {"name": MODEL, **model}}


def _build_config() -> dict[str, Any]:
    return {
        **_config(workload_type="GPU_SMALL"),
        "build": {"script": "./build.py", "files": ["./model.py"], "dependencies": ["mlflow==3.17.1"]},
    }


def _state(ws: _ServingClient) -> dict[str, Any]:
    return shared_state.load_state(ws, "demo", "model_serving", "churn", "dev")


def _deploy(sub: Path, ws: _ServingClient) -> int:
    return de.deploy(sub, "dev", dict(VARIABLES), ws=ws)


class TestConfig:
    def test_scaffold_validates(self, tmp_path: Path) -> None:
        sub = tmp_path / "example"
        sub.mkdir()
        (sub / "endpoint_config.yml").write_text(SCAFFOLD.read_text(encoding="utf-8"), encoding="utf-8")
        config = de.load_endpoint_config(sub, VARIABLES)
        assert config.endpoint_name == "demo-app-example-model"
        assert config.model.workload_type == "CPU"
        assert config.model.scale_to_zero is True

    @pytest.mark.parametrize(
        "raw",
        [
            _config(version="1", alias="champion"),
            {**_build_config(), "model": {"name": MODEL, "version": "2"}},
            {**_build_config(), "build": {"script": "build.py"}},
            {**_config(), "endpoint_name": "bad name"},
            _config(name="not-three-levels"),
        ],
    )
    def test_invalid_configs_are_rejected(self, raw: dict[str, Any]) -> None:
        with pytest.raises(ValueError):
            ServingEndpointConfig.model_validate(raw)

    def test_unresolved_variable_is_a_config_error(self, tmp_path: Path) -> None:
        sub = _sub_instance(tmp_path, {**_config(), "endpoint_name": "${var.missing}"})
        with pytest.raises(FoundryConfigError, match="missing"):
            de.load_endpoint_config(sub, VARIABLES)


class TestResolveVersion:
    def test_pinned_alias_and_latest(self) -> None:
        ws = _ServingClient()
        ws.model_versions.versions[MODEL] = [1, 3, 2]
        ws.model_versions.aliases[(MODEL, "champion")] = 2
        assert de.resolve_version(ws, ServingEndpointConfig.model_validate(_config(version="1"))) == "1"
        assert de.resolve_version(ws, ServingEndpointConfig.model_validate(_config(alias="champion"))) == "2"
        assert de.resolve_version(ws, ServingEndpointConfig.model_validate(_config())) == "3"

    def test_model_without_versions_fails(self) -> None:
        with pytest.raises(RuntimeError, match="no registered version"):
            de.resolve_version(_ServingClient(), ServingEndpointConfig.model_validate(_config()))


class TestServeExistingModel:
    def test_creates_a_cpu_endpoint_and_records_state(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        ws.model_versions.versions[MODEL] = [4]
        assert _deploy(_sub_instance(tmp_path, _config()), ws) == 0

        created = ws.serving_endpoints.created[0]
        entity = created["config"].served_entities[0]
        assert (entity.entity_name, entity.entity_version, entity.workload_type.value) == (MODEL, "4", "CPU")
        assert entity.scale_to_zero_enabled is True
        assert created["config"].name == "demo-churn"
        assert created["tags"]
        state = _state(ws)
        assert {key: state[key] for key in ("endpoint_name", "model_name", "model_version")} == {
            "endpoint_name": "demo-churn",
            "model_name": MODEL,
            "model_version": "4",
        }
        assert "build_hash" not in state
        assert ws.jobs.submitted == []

    def test_redeploy_of_the_same_version_does_not_update(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        ws.model_versions.versions[MODEL] = [4]
        sub = _sub_instance(tmp_path, _config())
        _deploy(sub, ws)
        _deploy(sub, ws)
        assert len(ws.serving_endpoints.created) == 1
        assert ws.serving_endpoints.updated == []

    def test_new_version_updates_the_endpoint(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        ws.model_versions.versions[MODEL] = [4]
        sub = _sub_instance(tmp_path, _config())
        _deploy(sub, ws)
        ws.model_versions.versions[MODEL].append(5)
        _deploy(sub, ws)
        assert ws.serving_endpoints.updated[0].entity_version == "5"

    def test_failed_create_prints_build_log_tail(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        ws = _ServingClient()
        ws.model_versions.versions[MODEL] = [1]
        ws.serving_endpoints.fail_create = True
        with pytest.raises(RuntimeError, match="container build failed"):
            _deploy(_sub_instance(tmp_path, _config()), ws)
        assert "ImportError: libcudnn" in capsys.readouterr().out
        assert _state(ws) == {}


class TestBuild:
    FILES = {"build.py": "print('build')\n", "model.py": "MODEL = 1\n"}

    def test_first_deploy_builds_and_serves_the_new_version(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        assert _deploy(_sub_instance(tmp_path, _build_config(), self.FILES), ws) == 0

        submitted = ws.jobs.submitted[0]
        task = submitted["tasks"][0]
        root = "/Shared/agent-foundry/dev/demo-app/model_serving/churn"
        assert task.spark_python_task.python_file == f"/Workspace{root}/build.py"
        assert task.spark_python_task.parameters == ["--model-name", MODEL, "--experiment", f"{root}/build_experiment"]
        assert submitted["environments"][0].spec.dependencies == ["mlflow==3.17.1"]
        assert {f"{root}/build.py", f"{root}/model.py"} <= set(ws.workspace.files)
        state = _state(ws)
        assert (state["model_version"], state["experiment_id"]) == ("1", "exp-1")
        assert ws.serving_endpoints.created[0]["config"].served_entities[0].workload_type.value == "GPU_SMALL"

    def test_unchanged_inputs_skip_the_build(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        sub = _sub_instance(tmp_path, _build_config(), self.FILES)
        _deploy(sub, ws)
        _deploy(sub, ws)
        assert len(ws.jobs.submitted) == 1

    def test_changed_model_code_rebuilds(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        sub = _sub_instance(tmp_path, _build_config(), self.FILES)
        _deploy(sub, ws)
        (sub / "model.py").write_text("MODEL = 2\n", encoding="utf-8")
        _deploy(sub, ws)
        assert len(ws.jobs.submitted) == 2
        assert ws.serving_endpoints.updated[0].entity_version == "2"

    def test_build_without_a_new_version_fails(self, tmp_path: Path) -> None:
        ws = _ServingClient(build_registers=False)
        with pytest.raises(RuntimeError, match="without registering"):
            _deploy(_sub_instance(tmp_path, _build_config(), self.FILES), ws)

    def test_build_source_outside_the_sub_instance_is_rejected(self, tmp_path: Path) -> None:
        raw = _build_config()
        raw["build"]["files"] = ["./../secret.py"]
        (tmp_path / "instances" / "demo" / "model_serving").mkdir(parents=True)
        (tmp_path / "instances" / "demo" / "model_serving" / "secret.py").write_text("x", encoding="utf-8")
        with pytest.raises(ValueError, match="escapes"):
            _deploy(_sub_instance(tmp_path, raw, self.FILES), _ServingClient())


class TestBrick:
    def _ctx(self, sub: Path, ws: _ServingClient) -> DeployContext:
        return DeployContext(instance_dir=sub.parent.parent, target="dev", variables=dict(VARIABLES), ws=ws)

    def test_publishes_the_endpoint_name(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        ws.model_versions.versions[MODEL] = [1]
        ctx = self._ctx(_sub_instance(tmp_path, _config()), ws)
        brick = ModelServingBrick()
        assert brick.applies_to(ctx)
        assert [result.status for result in brick.deploy(ctx)] == ["ok"]
        assert ctx.variables["serving_endpoint_name__churn"] == "demo-churn"

    def test_deploy_failure_is_a_deploy_error_result(self, tmp_path: Path) -> None:
        ctx = self._ctx(_sub_instance(tmp_path, _config()), _ServingClient())
        [result] = ModelServingBrick().deploy(ctx)
        assert (result.status, result.rc) == ("deploy_error", 1)

    def test_destroy_deletes_endpoint_built_model_and_experiment(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        sub = _sub_instance(tmp_path, _build_config(), TestBuild.FILES)
        _deploy(sub, ws)
        ctx = DestroyContext(instance_dir=sub.parent.parent, target="dev", variables=dict(VARIABLES), ws=ws, force=True)

        [result] = ModelServingBrick().destroy(ctx)

        assert result.status == "ok"
        assert {(ref.kind, ref.resource_id) for ref in result.deleted} == {
            ("serving_endpoint", "demo-churn"),
            ("registered_model", MODEL),
            ("mlflow_experiment", "exp-1"),
        }
        assert ws.serving_endpoints.deleted == ["demo-churn"]
        assert ws.model_versions.deleted == [MODEL]
        assert ws.experiments.deleted == ["exp-1"]
        assert _state(ws) == {}

    def test_destroy_keeps_a_model_it_did_not_build(self, tmp_path: Path) -> None:
        ws = _ServingClient()
        ws.model_versions.versions[MODEL] = [1]
        sub = _sub_instance(tmp_path, _config())
        _deploy(sub, ws)
        ctx = DestroyContext(instance_dir=sub.parent.parent, target="dev", variables=dict(VARIABLES), ws=ws, force=True)

        [result] = ModelServingBrick().destroy(ctx)

        assert [ref.kind for ref in result.deleted] == ["serving_endpoint"]
        assert ws.model_versions.deleted == []
