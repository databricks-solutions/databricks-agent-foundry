"""Tests for the ``rest_mcp`` payload-owned scaffold."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_foundry.scaffold import build_scaffold_registry, run_scaffold
from payloads.agent.config import resolve_agent_spec
from payloads.agent.deploy.mcp.validate import validate_server_yml
from payloads.rest_mcp.adapter import RestMcpPayloadAdapter
from payloads.rest_mcp.contracts import RestMcpPayloadFile


def test_adapter_declares_rest_mcp_scaffold() -> None:
    declarations = RestMcpPayloadAdapter().scaffolds()

    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration.name == "rest_mcp"
    assert declaration.group == "rest_mcp payload"
    assert {source.destination for source in declaration.sources} == {
        "payloads/rest_mcp/payload.yml",
        "payloads/agent/mcp_servers/example_api/server.yml",
    }
    assert all(source.source.is_file() for source in declaration.sources)


def test_registry_includes_rest_mcp_scaffold() -> None:
    assert "rest_mcp" in build_scaffold_registry()


def test_rest_mcp_scaffold_round_trips_through_owned_contract(tmp_path: Path) -> None:
    instance_dir = tmp_path / "instances" / "demo"
    instance_dir.mkdir(parents=True)

    result = run_scaffold("rest_mcp", instance_dir)

    payload_path = instance_dir / "payloads" / "rest_mcp" / "payload.yml"
    server_path = instance_dir / "payloads" / "agent" / "mcp_servers" / "example_api" / "server.yml"
    assert set(result.written) == {payload_path, server_path}

    raw = yaml.safe_load(payload_path.read_text(encoding="utf-8"))
    config = RestMcpPayloadFile.model_validate(raw)
    assert config.payload.adapter == "rest_mcp"
    assert config.payload.spec.apis[0].name == "example_api"

    _, errors, _ = validate_server_yml(
        server_path,
        {"rest_mcp_connection__example_api": "example_api_connection"},
    )
    assert errors == []


_GATEWAY_PIN_SPEC = {
    "runtime": {
        "tools": {
            "mcp": {
                "gateway": {
                    "pinned": [
                        {
                            "fqn": "${var.rest_mcp_service__example_api}",
                            "identity": "sp",
                            "tools": {"allow": ["get_item"]},
                        }
                    ]
                }
            }
        }
    }
}


def test_gateway_pin_resolves_from_rest_mcp_service_output() -> None:
    spec = resolve_agent_spec(
        None,
        _GATEWAY_PIN_SPEC,
        variables={"rest_mcp_service__example_api": "main.demo_rest_mcp.example_api"},
    )

    gateway = spec.runtime.tools.mcp.effective_gateway()
    assert gateway is not None
    assert [(pin.fqn, pin.identity, pin.tools.allow) for pin in gateway.pinned] == [
        ("main.demo_rest_mcp.example_api", "sp", ["get_item"])
    ]


def test_gateway_pin_fails_before_mcp_service_is_deployed() -> None:
    with pytest.raises(ValueError, match="rest_mcp_service__example_api"):
        resolve_agent_spec(None, _GATEWAY_PIN_SPEC, variables={})


def test_rest_mcp_scaffold_files_include_schema_headers() -> None:
    for declaration in RestMcpPayloadAdapter().scaffolds():
        for source in declaration.sources:
            body = source.source.read_text(encoding="utf-8")
            assert "yaml-language-server" in body
            assert ".agent-foundry/schemas/" in body
