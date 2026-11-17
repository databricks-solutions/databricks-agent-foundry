"""Labs User-Agent telemetry (product + presence extras)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from agent_foundry.bricks import BrickResult
from agent_foundry.public import DeployResult
from agent_foundry.telemetry import (
    PRODUCT_NAME,
    classify_llm_endpoint,
    extras_from_deploy,
    llm_model_id,
    log_extras,
    product_kwargs,
    reset_telemetry_cache,
    sanitize_extra_value,
    telemetry_disabled,
)


@pytest.fixture(autouse=True)
def _clear_telemetry_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_FOUNDRY_TELEMETRY_DISABLED", raising=False)
    reset_telemetry_cache()
    _FakeClient.pings = 0
    _FakeClient.extras = []
    _FakeClient.should_fail = False
    yield
    reset_telemetry_cache()
    _FakeClient.pings = 0
    _FakeClient.extras = []
    _FakeClient.should_fail = False


def _deploy_result(bricks: dict[str, list[BrickResult]]) -> DeployResult:
    return DeployResult(instance_name="demo", target="dev", bricks=bricks)


def test_product_kwargs_default() -> None:
    kw = product_kwargs()
    assert kw["product"] == PRODUCT_NAME
    assert kw["product_version"]
    assert kw["product_version"] != "0.0.0"


def test_product_kwargs_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FOUNDRY_TELEMETRY_DISABLED", "true")
    assert product_kwargs() == {}
    assert telemetry_disabled() is True


def test_nested_jobs_emit_single_brick_extra(tmp_path: Path) -> None:
    result = _deploy_result(
        {
            "Jobs": [
                BrickResult(name="ingest", status="ok"),
                BrickResult(name="refresh", status="ok"),
            ],
            "Vector Search": [
                BrickResult(name="docs", status="ok"),
                BrickResult(name="faq", status="ok"),
            ],
            "SQL Warehouse": [BrickResult(name="wh", status="skipped")],
        }
    )
    pairs = extras_from_deploy(result, tmp_path)
    bricks = [v for k, v in pairs if k == "brick"]
    assert bricks.count("jobs") == 1
    assert bricks.count("vector_indexes") == 1
    assert "warehouse" not in bricks
    assert ("cmd", "deploy") in pairs


_PIPELINE_ID = "70ec03e2-7c59-425b-bb71-a768b63c5c7b"
_ENDPOINT_ID = "21411cc2-9410-4224-8213-74edf0947081"


def _vs_result(name: str = "docs", status: str = "ok", **details: Any) -> BrickResult:
    return BrickResult(name=name, status=status, details=details)


def _vs_pairs(tmp_path: Path, *results: BrickResult) -> list[tuple[str, str]]:
    pairs = extras_from_deploy(_deploy_result({"Vector Search": list(results)}), tmp_path)
    return [(k, v) for k, v in pairs if k.startswith("vs_")]


def test_vs_dedicated_endpoint_and_pipeline_ids(tmp_path: Path) -> None:
    result = _vs_result(pipeline_id=_PIPELINE_ID, endpoint_id=_ENDPOINT_ID, endpoint_num_indexes=1)

    assert _vs_pairs(tmp_path, result) == [("vs_endpoint", _ENDPOINT_ID), ("vs_pipeline", _PIPELINE_ID)]


def test_vs_shared_endpoint_is_flagged(tmp_path: Path) -> None:
    result = _vs_result(endpoint_id=_ENDPOINT_ID, endpoint_num_indexes=4)

    assert _vs_pairs(tmp_path, result) == [("vs_endpoint_shared", _ENDPOINT_ID)]


def test_vs_ids_dedup_across_indexes(tmp_path: Path) -> None:
    shared: dict[str, Any] = {"endpoint_id": _ENDPOINT_ID, "endpoint_num_indexes": 2}

    pairs = _vs_pairs(tmp_path, _vs_result("docs", **shared), _vs_result("faq", **shared))

    assert pairs == [("vs_endpoint_shared", _ENDPOINT_ID)]


@pytest.mark.parametrize(
    "result",
    [
        _vs_result(status="deploy_error", pipeline_id=_PIPELINE_ID, endpoint_id=_ENDPOINT_ID),
        _vs_result(pipeline_id="docs_index", endpoint_id="vs_docs_demo"),
        _vs_result(),
    ],
    ids=["failed_deploy", "names_not_uuids", "no_ids"],
)
def test_vs_ids_skipped(tmp_path: Path, result: BrickResult) -> None:
    assert _vs_pairs(tmp_path, result) == []


def test_payload_and_mcp_presence(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text(
        yaml.dump(
            {
                "payload": {
                    "adapter": "agent",
                    "requires": ["tracing", "observability"],
                    "spec": {
                        "runtime": {"tools": {"mcp_only": False}},
                        "ui": {"chat_history": {"enabled": False}},
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    mcp = payload_dir / "mcp_servers"
    (mcp / "genie").mkdir(parents=True)
    (mcp / "genie" / "server.yml").write_text(
        "type: managed_genie\ntools:\n  allow: ['query_space_*']\n",
        encoding="utf-8",
    )
    (mcp / "vs").mkdir()
    (mcp / "vs" / "server.yml").write_text(
        "type: managed_vector_search\ntools:\n  allow: ['*']\n",
        encoding="utf-8",
    )
    (mcp / "vs2").mkdir()
    (mcp / "vs2" / "server.yml").write_text(
        "type: managed_vector_search\ntools:\n  allow: ['*']\n",
        encoding="utf-8",
    )
    (mcp / "gateway").mkdir()
    (mcp / "gateway" / "server.yml").write_text(
        "type: ai_gateway\ntools:\n  allow: ['*']\n",
        encoding="utf-8",
    )
    pairs = extras_from_deploy(_deploy_result({}), tmp_path)
    mcp_types = [v for k, v in pairs if k == "mcp"]
    assert mcp_types.count("managed_genie") == 1
    assert mcp_types.count("managed_vector_search") == 1
    assert mcp_types.count("ai_gateway") == 1
    assert ("payload", "agent") in pairs
    assert ("mcp_only", "false") in pairs
    assert ("lakebase", "false") in pairs


def test_payload_adapter_passthrough_no_allowlist(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads" / "support_desk"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text(
        yaml.dump(
            {
                "payload": {
                    "adapter": "support_desk",
                    "spec": {
                        "runtime": {"model": {"kind": "serving_endpoint", "name": "databricks-claude-sonnet-4-6"}}
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    pairs = extras_from_deploy(_deploy_result({}), tmp_path)
    assert ("payload", "support_desk") in pairs
    assert not any(k == "mcp_only" for k, _ in pairs)
    assert ("llm", "fmapi") in pairs


def _write_agent_payload(root: Path, endpoint: str) -> None:
    payload_dir = root / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    (payload_dir / "payload.yml").write_text(
        yaml.dump(
            {
                "payload": {
                    "adapter": "agent",
                    "requires": ["tracing", "observability"],
                    "spec": {"runtime": {"model": {"kind": "serving_endpoint", "name": endpoint}}},
                }
            }
        ),
        encoding="utf-8",
    )


def test_classify_external_openai() -> None:
    endpoint = {
        "config": {
            "served_entities": [
                {"external_model": {"provider": "openai", "name": "gpt-4"}},
            ]
        }
    }
    assert classify_llm_endpoint("my-gpt", endpoint) == ("external", "openai")


def test_classify_throughput() -> None:
    endpoint = {
        "config": {
            "served_entities": [
                {
                    "entity_name": "system.ai.meta_llama_v3_3_70b_instruct",
                    "min_provisioned_throughput": 0,
                    "max_provisioned_throughput": 10800,
                }
            ]
        }
    }
    assert classify_llm_endpoint("prod-llama", endpoint) == ("throughput", "databricks")


def test_classify_fmapi_pay_per_token() -> None:
    endpoint = {
        "config": {
            "served_entities": [
                {"entity_name": "system.ai.anthropic_claude_sonnet_4_6"},
            ]
        }
    }
    assert classify_llm_endpoint("databricks-claude-sonnet-4-6", endpoint) == (
        "fmapi",
        "databricks",
    )


def test_classify_custom() -> None:
    endpoint = {"config": {"served_entities": [{"entity_name": "main.ml.my_agent", "workload_size": "Small"}]}}
    assert classify_llm_endpoint("my-custom-ep", endpoint) == ("custom", "custom")


def test_classify_provider_passthrough_no_allowlist() -> None:
    endpoint = {"config": {"served_entities": [{"external_model": {"provider": "ai21labs"}}]}}
    assert classify_llm_endpoint("ext", endpoint) == ("external", "ai21labs")
    future = {
        "config": {
            "served_entities": [{"external_model": {"provider": "Mistral_AI"}}],
        }
    }
    assert classify_llm_endpoint("ext", future) == ("external", "mistral-ai")


def test_classify_model_service_is_unity_gateway() -> None:
    assert classify_llm_endpoint("system.ai.claude-sonnet-4-5", None, model_kind="model_service") == (
        "unity_gateway",
        "databricks",
    )


def test_classify_serving_endpoint_kind_is_unchanged() -> None:
    assert classify_llm_endpoint("databricks-x", None, model_kind="serving_endpoint") == ("fmapi", "databricks")


def test_model_service_payload_reports_unity_gateway_without_endpoint_lookup(tmp_path: Path) -> None:
    payload_dir = tmp_path / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    spec = {"runtime": {"model": {"kind": "model_service", "name": "system.ai.claude-sonnet-4-5"}}}
    (payload_dir / "payload.yml").write_text(
        yaml.dump({"payload": {"adapter": "agent", "spec": spec}}),
        encoding="utf-8",
    )

    class _NoServingEndpoints:
        @property
        def serving_endpoints(self) -> Any:
            raise AssertionError("a model service is not a serving endpoint")

    pairs = extras_from_deploy(_deploy_result({}), tmp_path, _NoServingEndpoints())
    assert ("llm", "unity_gateway") in pairs
    assert ("llm_provider", "databricks") in pairs


def test_llm_model_id_from_external_name() -> None:
    endpoint = {
        "config": {
            "served_entities": [
                {"external_model": {"provider": "openai", "name": "gpt-5.4-preview"}},
            ]
        }
    }
    assert llm_model_id("my-gpt", endpoint) == "gpt-5.4-preview"


def test_llm_model_id_from_uc_leaf() -> None:
    endpoint = {
        "config": {
            "served_entities": [
                {"entity_name": "system.ai.anthropic_claude_sonnet_4_6"},
            ]
        }
    }
    assert llm_model_id("databricks-claude-sonnet-4-6", endpoint) == "anthropic_claude_sonnet_4_6"


def test_llm_model_id_sanitizes_without_allowlist() -> None:
    assert sanitize_extra_value("accounts/org/models/foo bar") == "accounts-org-models-foo-bar"
    endpoint = {
        "config": {
            "served_entities": [
                {"external_model": {"provider": "custom", "name": "my-org/claude:opus"}},
            ]
        }
    }
    assert llm_model_id("proxy", endpoint) == "my-org-claude-opus"


def test_llm_extras_from_get(tmp_path: Path) -> None:
    _write_agent_payload(tmp_path, "openai_chat")

    class _Serving:
        def get(self, name: str) -> dict[str, Any]:
            assert name == "openai_chat"
            return {
                "config": {
                    "served_entities": [
                        {"external_model": {"provider": "anthropic", "name": "claude-sonnet-4-6"}},
                    ]
                },
                "ai_gateway": {"usage_tracking_config": {"enabled": True}},
            }

    class _Ws:
        serving_endpoints = _Serving()

    pairs = extras_from_deploy(_deploy_result({}), tmp_path, _Ws())
    assert ("llm", "external") in pairs
    assert ("llm_provider", "anthropic") in pairs
    assert ("llm_model", "claude-sonnet-4-6") in pairs
    assert ("llm_gateway", "true") in pairs


def test_llm_extras_prefix_without_client(tmp_path: Path) -> None:
    _write_agent_payload(tmp_path, "databricks-meta-llama-3-3-70b-instruct")
    pairs = extras_from_deploy(_deploy_result({}), tmp_path)
    assert ("llm", "fmapi") in pairs
    assert ("llm_provider", "databricks") in pairs
    assert ("llm_model", "databricks-meta-llama-3-3-70b-instruct") in pairs
    assert not any(k == "llm_gateway" for k, _ in pairs)


def test_llm_extras_get_failure_falls_back(tmp_path: Path) -> None:
    _write_agent_payload(tmp_path, "customer-endpoint")

    class _Serving:
        def get(self, name: str) -> dict[str, Any]:
            raise RuntimeError("404")

    class _Ws:
        serving_endpoints = _Serving()

    pairs = extras_from_deploy(_deploy_result({}), tmp_path, _Ws())
    assert ("llm", "unknown") in pairs
    assert ("llm_provider", "unknown") in pairs
    assert ("llm_model", "customer-endpoint") in pairs
    assert not any(k == "llm_gateway" for k, _ in pairs)


def test_invalid_extras_are_dropped() -> None:
    ws = _FakeClient()
    log_extras(ws, [("brick", "not valid"), ("cmd", "deploy")])
    assert _FakeClient.pings == 1
    assert ("cmd", "deploy") in _FakeClient.extras
    assert ("brick", "not valid") not in _FakeClient.extras


def test_log_extras_swallows_errors() -> None:
    _FakeClient.should_fail = True
    log_extras(_FakeClient(), [("cmd", "deploy")])


def test_log_extras_dedups() -> None:
    ws = _FakeClient()
    log_extras(ws, [("cmd", "deploy")])
    log_extras(ws, [("cmd", "deploy")])
    assert _FakeClient.pings == 1


def test_disabled_skips_ping(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FOUNDRY_TELEMETRY_DISABLED", "1")
    log_extras(_FakeClient(), [("cmd", "deploy")])
    assert _FakeClient.pings == 0


def test_emit_command_noops_without_ws() -> None:
    from agent_foundry.telemetry import emit_command

    emit_command("init")
    emit_command("eval")
    assert _FakeClient.pings == 0


def test_emit_command_pings_when_ws_given() -> None:
    from agent_foundry.telemetry import emit_command

    emit_command("init", ws=_FakeClient())
    assert _FakeClient.pings == 1
    assert ("cmd", "init") in _FakeClient.extras


class _FakeConfig:
    def __init__(self) -> None:
        self.recorded: list[tuple[str, str]] = []

    def copy(self) -> _FakeConfig:
        clone = _FakeConfig()
        clone.recorded = list(self.recorded)
        return clone

    def with_user_agent_extra(self, key: str, value: str) -> _FakeConfig:
        self.recorded.append((key, value))
        return self


class _FakeClient:
    pings = 0
    extras: list[tuple[str, str]] = []
    should_fail = False

    def __init__(self, config: Any = None, **_: Any) -> None:
        self.config = config or _FakeConfig()

    @property
    def current_user(self) -> _FakeClient:
        return self

    def me(self) -> object:
        type(self).pings += 1
        type(self).extras.extend(list(getattr(self.config, "recorded", [])))
        if type(self).should_fail:
            raise RuntimeError("workspace down")
        return object()
