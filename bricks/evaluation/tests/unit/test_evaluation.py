"""
Offline unit tests for evaluation config loading, dataset parsing,
and scorer resolution.

These tests verify the runner infrastructure without requiring
MLflow or a Databricks workspace.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

BRICK_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BRICK_ROOT.parent.parent


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestEvalConfig:
    def test_default_config_exists(self) -> None:
        config_path = BRICK_ROOT / "conf" / "eval_config.yml"
        assert config_path.exists(), f"Default eval config missing: {config_path}"

    def test_default_config_valid_yaml(self) -> None:
        config_path = BRICK_ROOT / "conf" / "eval_config.yml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        assert isinstance(config, dict)
        assert "scorers" in config
        assert "production_monitoring" in config

    def test_config_has_required_sections(self) -> None:
        config_path = BRICK_ROOT / "conf" / "eval_config.yml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        scorers = config["scorers"]
        assert "default" in scorers
        assert "with_ground_truth" in scorers
        assert "custom" in scorers

        prod = config["production_monitoring"]
        assert "sample_rate" in prod
        assert "scorers" in prod
        assert 0 < prod["sample_rate"] <= 1.0

    def test_config_scorer_names_valid(self) -> None:
        config_path = BRICK_ROOT / "conf" / "eval_config.yml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        known_builtin = {
            "Safety",
            "RelevanceToQuery",
            "RetrievalGroundedness",
            "Correctness",
            "ToolCallCorrectness",
            "Guidelines",
        }
        known_custom = {
            "tool_routing_accuracy",
            "genie_sql_correctness",
        }
        all_known = known_builtin | known_custom

        for section in ["default", "with_ground_truth", "traces_only", "custom"]:
            for name in config["scorers"].get(section, []):
                assert name in all_known, f"Unknown scorer '{name}' in scorers.{section}"

        assert config["scorers"].get("traces_only", []) == [], (
            "RetrievalGroundedness must stay opt-in (issue #61); default traces_only should be empty"
        )

        for name in config["production_monitoring"].get("scorers", []):
            assert name in all_known, f"Unknown scorer '{name}' in production_monitoring"

    def test_default_judges_preset_omits_retrieval_groundedness(self) -> None:
        from bricks.evaluation.scorers.builtin import DEFAULT_JUDGES

        assert "RetrievalGroundedness" not in DEFAULT_JUDGES
        assert DEFAULT_JUDGES == ["Safety", "RelevanceToQuery"]

    def test_experiment_name_derived_from_variables(self) -> None:
        """experiment_name is no longer in the brick default; it's computed
        from tracing_experiment by the evaluation runner."""
        config_path = BRICK_ROOT / "conf" / "eval_config.yml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        assert "experiment_name" not in config, "experiment_name should be derived, not in the brick default"

    @pytest.mark.parametrize(
        ("config", "variables", "expected"),
        [
            ({"experiment_name": "/Shared/explicit"}, {"tracing_experiment": "/Shared/traces"}, "/Shared/explicit"),
            ({}, {"tracing_experiment": "/Shared/traces"}, "/Shared/traces"),
            ({}, None, "agent_evaluation"),
        ],
    )
    def test_get_experiment_name_precedence(
        self,
        config: dict[str, Any],
        variables: dict[str, str] | None,
        expected: str,
    ) -> None:
        from bricks.evaluation.config import get_experiment_name

        assert get_experiment_name(config, variables) == expected

    @pytest.mark.parametrize("action", ["action_stop", "action_status", "action_delete"])
    def test_monitor_actions_use_tracing_experiment(self, monkeypatch: pytest.MonkeyPatch, action: str) -> None:
        import mlflow.genai.scorers

        from bricks.evaluation.scripts import register_monitors as mod

        seen: list[str] = []
        monkeypatch.setattr(mod, "_init_mlflow_experiment", lambda name, _variables: seen.append(name))
        monkeypatch.setattr(mlflow.genai.scorers, "list_scorers", lambda: [])

        getattr(mod, action)({}, variables={"tracing_experiment": "/Shared/traces"})

        assert seen == ["/Shared/traces"]

    def test_prod_monitoring_scorers_need_no_expectations(self) -> None:
        """Production monitoring scorers must not depend on expectations
        (ground truth) because it is unavailable for live traces."""
        expectations_scorers = {"tool_routing_accuracy", "genie_sql_correctness"}
        config_path = BRICK_ROOT / "conf" / "eval_config.yml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        for name in config["production_monitoring"].get("scorers", []):
            assert name not in expectations_scorers, (
                f"Scorer '{name}' requires expectations and cannot run in production monitoring"
            )


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------


class TestDatasetLoading:
    def test_template_exists(self) -> None:
        template = BRICK_ROOT / "datasets" / "_template.yml"
        assert template.exists(), f"Dataset template missing: {template}"

    def test_template_valid_yaml(self) -> None:
        template = BRICK_ROOT / "datasets" / "_template.yml"
        with open(template, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert isinstance(data, dict)
        assert "dataset" in data

    def test_template_records_have_inputs(self) -> None:
        template = BRICK_ROOT / "datasets" / "_template.yml"
        with open(template, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for i, record in enumerate(data["dataset"]):
            assert "inputs" in record, f"Record {i} missing 'inputs'"
            assert isinstance(record["inputs"], dict)

    def test_template_expectations_format(self) -> None:
        template = BRICK_ROOT / "datasets" / "_template.yml"
        with open(template, encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for i, record in enumerate(data["dataset"]):
            if "expectations" in record:
                exp = record["expectations"]
                assert isinstance(exp, dict), f"Record {i}: expectations must be dict"
                if "expected_tools" in exp:
                    assert isinstance(exp["expected_tools"], list)

    def test_template_request_level_inputs_row_parses(self) -> None:
        from bricks.evaluation.scripts.run_evaluation import _request_extras, load_dataset_from_yaml

        records = load_dataset_from_yaml(BRICK_ROOT / "datasets" / "_template.yml")
        seeded = [r for r in records if "custom_inputs" in r["inputs"]]
        assert seeded, "template should show one row with inputs.custom_inputs"
        extras = _request_extras(seeded[0]["inputs"])
        assert set(extras) == {"custom_inputs", "context"}
        assert all(isinstance(v, dict) for v in extras.values())


# ---------------------------------------------------------------------------
# Variable substitution in record expectations
# ---------------------------------------------------------------------------


class TestSubstituteRecordExpectations:
    """Cover the narrow ${var.<name>} substitution applied to dataset records.

    Substitution must:
      * resolve placeholders inside ``expectations`` (including nested values
        like ``expected_sql``),
      * leave ``inputs`` untouched (we want the literal user question),
      * be a no-op for datasets that contain no ``${var.…}`` references,
      * tolerate missing variables without raising (``strict=False``).
    """

    def test_resolves_var_in_expected_sql(self) -> None:
        from bricks.evaluation.scripts.run_evaluation import (
            substitute_record_expectations,
        )

        records = [
            {
                "inputs": {"question": "How many initiatives are open?"},
                "expectations": {
                    "expected_sql": "SELECT * FROM ${var.catalog}.foo",
                    "expected_tools": ["ask_swf_portfolio_planning"],
                },
            }
        ]
        out = substitute_record_expectations(records, {"catalog": "main"})

        assert out[0]["expectations"]["expected_sql"] == "SELECT * FROM main.foo"
        assert out[0]["expectations"]["expected_tools"] == ["ask_swf_portfolio_planning"]

    def test_no_placeholders_is_identity(self) -> None:
        from bricks.evaluation.scripts.run_evaluation import (
            substitute_record_expectations,
        )

        records: list[dict[str, Any]] = [
            {
                "inputs": {"question": "Hello"},
                "expectations": {"expected_tools": ["genie"]},
            },
            {"inputs": {"question": "No expectations here"}},
        ]
        out = substitute_record_expectations(records, {"catalog": "main"})

        assert out == records

    def test_inputs_question_is_left_untouched(self) -> None:
        """Substitution scope contract: only ``expectations`` is templated."""
        from bricks.evaluation.scripts.run_evaluation import (
            substitute_record_expectations,
        )

        records = [
            {
                "inputs": {"question": "What is in ${var.foo}?"},
                "expectations": {"expected_sql": "SELECT 1 FROM ${var.catalog}.t"},
            }
        ]
        out = substitute_record_expectations(
            records,
            {"catalog": "main", "foo": "bar"},
        )

        assert out[0]["inputs"]["question"] == "What is in ${var.foo}?"
        assert out[0]["expectations"]["expected_sql"] == "SELECT 1 FROM main.t"

    def test_missing_variable_is_left_in_place(self) -> None:
        """``strict=False`` => unknown placeholders survive without raising."""
        from bricks.evaluation.scripts.run_evaluation import (
            substitute_record_expectations,
        )

        records = [
            {
                "inputs": {"question": "Q"},
                "expectations": {"expected_sql": "SELECT * FROM ${var.unknown}.t"},
            }
        ]
        out = substitute_record_expectations(records, {"catalog": "main"})

        assert out[0]["expectations"]["expected_sql"] == "SELECT * FROM ${var.unknown}.t"


# ---------------------------------------------------------------------------
# MLflow auth env (#421)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name",
    ["bricks.evaluation.scripts.run_evaluation", "bricks.evaluation.scripts.register_monitors"],
)
class TestInitMlflowExperimentAuth:
    """Regression for #421: both evaluation entry points must pin the
    resolved profile/host in the environment *before* any ``mlflow.*`` call.

    MLflow builds its own ``WorkspaceClient`` from ambient default auth and
    never derives it from the tracking URI, so ``mlflow.get_trace`` (UC OTel
    reads) and monitor registration otherwise land on the ``[DEFAULT]``
    profile's workspace -- a 401 when profiles share a host, and a silent
    wrong-workspace read when they don't.
    """

    @staticmethod
    def _init_with_stub(
        module_name: str,
        monkeypatch: pytest.MonkeyPatch,
        variables: dict[str, str],
    ) -> list[tuple[str, str, str]]:
        """Call the module's ``_init_mlflow_experiment`` against a stubbed
        ``mlflow`` and return ``(call, argument, profile_env_at_call)``.
        """
        import importlib
        import sys

        calls: list[tuple[str, str, str]] = []

        def _record(name: str, arg: str) -> None:
            calls.append((name, arg, os.environ.get("DATABRICKS_CONFIG_PROFILE", "")))

        class _MlflowStub:
            def set_tracking_uri(self, uri: str) -> None:
                _record("set_tracking_uri", uri)

            def set_experiment(self, experiment_name: str, **_kwargs: Any) -> None:
                _record("set_experiment", experiment_name)

        monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
        monkeypatch.delenv("DATABRICKS_HOST", raising=False)
        monkeypatch.setitem(sys.modules, "mlflow", _MlflowStub())

        module = importlib.import_module(module_name)
        module._init_mlflow_experiment("exp-1", variables)
        return calls

    def test_exports_profile_before_first_mlflow_call(
        self,
        module_name: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = self._init_with_stub(
            module_name,
            monkeypatch,
            {"profile": "prof-x", "host": "https://ws.example"},
        )

        assert calls, "expected at least one mlflow call"
        assert calls[0][2] == "prof-x", f"profile must be exported before the first mlflow call; got {calls}"
        assert os.environ["DATABRICKS_HOST"] == "https://ws.example"

    def test_tracking_uri_carries_the_profile(
        self,
        module_name: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = self._init_with_stub(module_name, monkeypatch, {"profile": "prof-x"})

        uris = [arg for name, arg, _ in calls if name == "set_tracking_uri"]
        assert uris == ["databricks://prof-x"]

    def test_no_profile_leaves_env_untouched(
        self,
        module_name: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = self._init_with_stub(module_name, monkeypatch, {})

        assert "DATABRICKS_CONFIG_PROFILE" not in os.environ
        uris = [arg for name, arg, _ in calls if name == "set_tracking_uri"]
        assert uris == ["databricks"]


# ---------------------------------------------------------------------------
# Directory structure
# ---------------------------------------------------------------------------


class TestBrickStructure:
    def test_scorers_package_exists(self) -> None:
        assert (BRICK_ROOT / "scorers" / "__init__.py").exists()

    def test_all_scorer_modules_exist(self) -> None:
        for module in ["builtin.py", "routing.py", "genie_accuracy.py"]:
            path = BRICK_ROOT / "scorers" / module
            assert path.exists(), f"Missing scorer module: {path}"

    def test_scripts_exist(self) -> None:
        for script in ["run_evaluation.py", "register_monitors.py"]:
            path = BRICK_ROOT / "scripts" / script
            assert path.exists(), f"Missing script: {path}"

    def test_readme_exists(self) -> None:
        assert (BRICK_ROOT / "README.md").exists()

    def test_field_guide_exists(self) -> None:
        # The per-brick ``docs/field-guide.md`` was consolidated into the
        # brick's authored ``README.md`` (the single "Technical reference"
        # per brick).  Assert that consolidated reference is present.
        readme = BRICK_ROOT / "README.md"
        assert readme.exists()
        assert "Technical reference" in readme.read_text(encoding="utf-8")


def test_resolve_trace_warehouse_id_prefers_tracing_then_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_foundry import env
    from bricks.evaluation.scripts.run_evaluation import _resolve_trace_warehouse_id

    monkeypatch.delenv(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name, raising=False)
    assert _resolve_trace_warehouse_id({"tracing_warehouse_id": "tr-1", "warehouse_id": "wh-2"}) == "tr-1"
    assert _resolve_trace_warehouse_id({"warehouse_id": "wh-2"}) == "wh-2"
    monkeypatch.setenv(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name, "env-wh")
    assert _resolve_trace_warehouse_id({}) == "env-wh"
    monkeypatch.delenv(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name, raising=False)
    assert _resolve_trace_warehouse_id({}) == ""


def test_fetch_deployed_traces_skips_http_without_warehouse(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_foundry import env
    from bricks.evaluation.scripts import run_evaluation as mod

    monkeypatch.delenv(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name, raising=False)

    class _Ws:
        def __init__(self) -> None:
            self.warehouses = self

        def start_and_wait(self, id: str) -> None:  # noqa: A002
            raise AssertionError(f"must not start warehouse {id}")

        def get(self, name: str) -> None:
            raise AssertionError(f"must not look up app {name}")

    assert mod._fetch_deployed_traces([{"inputs": {"question": "hi"}}], None, {}, ws=_Ws()) == []


def test_fetch_deployed_traces_requires_injected_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    from bricks.evaluation.scripts.run_evaluation import _fetch_deployed_traces

    constructed: list[object] = []

    def _boom(*_a: object, **_k: object) -> None:
        constructed.append(1)
        raise AssertionError("must not construct a workspace client")

    monkeypatch.setattr("agent_foundry.workspace_client.get_workspace_client", _boom)
    with pytest.raises(RuntimeError, match="workspace client required"):
        _fetch_deployed_traces([], None, {})
    assert constructed == []


def test_run_direct_evaluation_forwards_injected_ws(monkeypatch: pytest.MonkeyPatch) -> None:
    from bricks.evaluation.scripts import run_evaluation as mod

    captured: dict[str, object] = {}
    sentinel = object()

    def _fake_fetch(
        records: list[dict[str, Any]],
        instance_dir: Path | None,
        variables: dict[str, str],
        *,
        ws: object | None = None,
    ) -> list[dict[str, Any]]:
        captured["ws"] = ws
        return []

    monkeypatch.setattr(mod, "_init_mlflow_experiment", lambda *_a, **_k: None)
    monkeypatch.setattr(mod, "resolve_scorers", lambda *_a, **_k: [])
    monkeypatch.setattr(mod, "find_datasets", lambda *_a, **_k: [Path("dummy.yml")])
    monkeypatch.setattr(mod, "load_dataset_from_yaml", lambda *_a, **_k: [{"inputs": {"q": "hi"}}])
    monkeypatch.setattr(mod, "substitute_record_expectations", lambda records, _v: records)
    monkeypatch.setattr(mod, "_fetch_deployed_traces", _fake_fetch)

    rc = mod.run_direct_evaluation(
        {"scorers": {"default": []}},
        None,
        {},
        predict_via="deployed",
        ws=sentinel,
    )
    assert rc == 1
    assert captured["ws"] is sentinel


# ---------------------------------------------------------------------------
# Request-level inputs (custom_inputs / context) forwarding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inputs", "expected", "warns"),
    [
        ({"question": "q"}, {}, False),
        ({"question": "q", "custom_inputs": None}, {}, False),
        (
            {"question": "q", "custom_inputs": {"spec": {"a": 1}}, "context": {"conversation_id": "c"}},
            {"custom_inputs": {"spec": {"a": 1}}, "context": {"conversation_id": "c"}},
            False,
        ),
        ({"question": "q", "custom_inputs": '{"spec": {"a": 1}}'}, {"custom_inputs": {"spec": {"a": 1}}}, False),
        ({"question": "q", "custom_inputs": "oops"}, {}, True),
        ({"question": "q", "context": [1]}, {}, True),
        # Missing shapes a pandas/UC column may use: no warning, like None.
        ({"question": "q", "custom_inputs": float("nan")}, {}, False),
        ({"question": "q", "custom_inputs": "null"}, {}, False),
        # YAML dates are normalised to strings so both predict modes agree.
        (
            {"question": "q", "custom_inputs": {"start": datetime.date(2024, 1, 1)}},
            {"custom_inputs": {"start": "2024-01-01"}},
            False,
        ),
        # ``context`` must validate as ChatContext (conversation_id is a str).
        ({"question": "q", "context": {"conversation_id": 123}}, {}, True),
        ({"question": "q", "context": {"user_id": "u"}}, {"context": {"user_id": "u"}}, False),
    ],
)
def test_request_extras(
    inputs: dict[str, Any],
    expected: dict[str, Any],
    warns: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bricks.evaluation.scripts.run_evaluation import _request_extras

    assert _request_extras(inputs) == expected
    assert ("WARNING: ignoring inputs." in capsys.readouterr().out) is warns


def test_request_extras_copies_nested_mappings() -> None:
    """The agent may mutate its seed in place; that must not leak into the row."""
    from bricks.evaluation.scripts.run_evaluation import _request_extras

    inputs = {"question": "q", "custom_inputs": {"spec": {"widgets": [1, 2]}}}
    extras = _request_extras(inputs)
    extras["custom_inputs"]["spec"]["widgets"].pop()
    assert inputs["custom_inputs"] == {"spec": {"widgets": [1, 2]}}


def test_request_extras_accepts_non_dict_mappings() -> None:
    """A Mapping that is not a ``dict`` (e.g. a Spark ``Row``) is not dropped."""
    from collections.abc import Mapping

    from bricks.evaluation.scripts.run_evaluation import _request_extras

    class _RowLike(Mapping[str, Any]):
        def __init__(self, data: dict[str, Any]) -> None:
            self._data = data

        def __getitem__(self, key: str) -> Any:
            return self._data[key]

        def __iter__(self) -> Iterator[str]:
            return iter(self._data)

        def __len__(self) -> int:
            return len(self._data)

    inputs = {"question": "q", "custom_inputs": _RowLike({"a": 1})}
    assert _request_extras(inputs) == {"custom_inputs": {"a": 1}}


def test_request_extras_preserves_numpy_scalar_types() -> None:
    """UC/pandas nested maps often carry numpy scalars; they must not stringify."""
    numpy = pytest.importorskip("numpy")
    from bricks.evaluation.scripts.run_evaluation import _request_extras

    inputs = {"question": "q", "custom_inputs": {"count": numpy.int64(1), "ratio": numpy.float64(0.5)}}
    assert _request_extras(inputs) == {"custom_inputs": {"count": 1, "ratio": 0.5}}


class _DeployedWs:
    """Just enough of a WorkspaceClient for ``_fetch_deployed_traces``."""

    def __init__(self) -> None:
        self.warehouses = self
        self.apps = self
        self.config = self
        self.url = "https://app.example.com/"

    def start_and_wait(self, id: str) -> None:  # noqa: A002
        pass

    def get(self, name: str) -> _DeployedWs:
        return self

    def authenticate(self) -> dict[str, str]:
        return {}


def _run_deployed_fetch(
    monkeypatch: pytest.MonkeyPatch,
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run ``_fetch_deployed_traces`` offline; return (POST payloads, emitted records)."""
    import json

    import mlflow
    import requests

    from agent_foundry import env
    from bricks.evaluation.scripts import run_evaluation as mod

    payloads: list[dict[str, Any]] = []

    # ``_fetch_deployed_traces`` writes the warehouse id straight into
    # ``os.environ``; register it with monkeypatch so teardown restores it.
    monkeypatch.setenv(env.MLFLOW_TRACING_SQL_WAREHOUSE_ID.name, "")

    class _Resp:
        content = json.dumps({"metadata": {"trace_id": "tr-1"}}).encode()

        def raise_for_status(self) -> None:
            pass

    def _fake_post(url: str, *, json: dict[str, Any], **_k: object) -> _Resp:
        payloads.append(json)
        return _Resp()

    monkeypatch.setattr(requests, "post", _fake_post)
    monkeypatch.setattr(mlflow, "get_trace", lambda *_a, **_k: "trace")
    monkeypatch.setattr(mod, "_TRACE_POLL_INTERVALS_S", (0,))
    enriched = mod._fetch_deployed_traces(
        records,
        None,
        {"warehouse_id": "wh-1", "app_name": "app"},
        ws=_DeployedWs(),
    )
    return payloads, enriched


def test_fetch_deployed_traces_forwards_custom_inputs_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = {
        "question": "q",
        "custom_inputs": {"spec": {"widgets": [1, 2]}},
        "context": {"conversation_id": "eval-123"},
    }
    payloads, enriched = _run_deployed_fetch(monkeypatch, [{"inputs": inputs, "expectations": {}}])

    assert payloads == [
        {
            "input": [{"role": "user", "content": "q"}],
            "stream": False,
            "custom_inputs": {"spec": {"widgets": [1, 2]}},
            "context": {"conversation_id": "eval-123"},
        }
    ]
    assert enriched[0]["inputs"] is inputs


def test_fetch_deployed_traces_plain_row_payload_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads, _ = _run_deployed_fetch(monkeypatch, [{"inputs": {"question": "q"}}])
    assert payloads == [{"input": [{"role": "user", "content": "q"}], "stream": False}]


def test_fetch_deployed_traces_drops_non_mapping_custom_inputs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    payloads, _ = _run_deployed_fetch(monkeypatch, [{"inputs": {"question": "q", "custom_inputs": 42}}])
    assert payloads == [{"input": [{"role": "user", "content": "q"}], "stream": False}]
    assert "WARNING: ignoring inputs.custom_inputs (expected a mapping, got int)" in capsys.readouterr().out


def test_local_predict_fn_forwards_custom_inputs_and_context(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    import mlflow.genai.agent_server

    from bricks.evaluation.scripts import run_evaluation as mod

    captured: list[Any] = []

    def _fake_invoke(req: Any) -> dict[str, Any]:
        captured.append(req)
        return {"output": []}

    # Keep the real agent module (and its tool wiring) out of the test.
    monkeypatch.setitem(sys.modules, "payloads.agent.runtime.agent", types.ModuleType("payloads.agent.runtime.agent"))
    monkeypatch.setattr(mlflow.genai.agent_server, "get_invoke_function", lambda: _fake_invoke)

    predict_fn = mod._build_local_predict_fn(None, {})

    predict_fn(question="q", custom_inputs={"spec": {"a": 1}}, context={"conversation_id": "c"})
    seeded = captured[-1]
    assert seeded.custom_inputs == {"spec": {"a": 1}}
    assert seeded.context.conversation_id == "c"

    predict_fn(question="q")
    plain = captured[-1]
    assert plain.custom_inputs is None
    assert plain.context is None


def test_local_predict_fn_ignores_unknown_input_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """A UC struct that grows a field beyond question/custom_inputs/context must not TypeError."""
    import sys
    import types

    import mlflow.genai.agent_server

    from bricks.evaluation.scripts import run_evaluation as mod

    captured: list[Any] = []

    def _fake_invoke(req: Any) -> dict[str, Any]:
        captured.append(req)
        return {"output": []}

    monkeypatch.setitem(sys.modules, "payloads.agent.runtime.agent", types.ModuleType("payloads.agent.runtime.agent"))
    monkeypatch.setattr(mlflow.genai.agent_server, "get_invoke_function", lambda: _fake_invoke)

    predict_fn = mod._build_local_predict_fn(None, {})

    predict_fn(question="q", new_field="unexpected")
    assert captured[-1].custom_inputs is None
