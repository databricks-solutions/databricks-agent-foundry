"""``agent-foundry brick mcp resolve``: policy and resolved set from the CLI."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from databricks.sdk.service.catalog import McpService

from payloads.agent.deploy.mcp import resolve as cli
from payloads.agent.runtime.gateway_resolve import clear_listing_cache

GATEWAY = {
    "pinned": [{"fqn": "team.pinned.jira", "identity": "sp", "tools": {"allow": ["search_*"]}}],
    "discovery": {"enabled": True, "locations": ["team.mcp"]},
    "user_catalog": {"enabled": True, "locations": ["team.mine"], "exclude": ["team.mine.gmail"]},
}


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_listing_cache()
    yield
    clear_listing_cache()


def _instance(tmp_path: Path, mcp: dict[str, object]) -> Path:
    payload_dir = tmp_path / "instances" / "demo" / "payloads" / "agent"
    payload_dir.mkdir(parents=True)
    spec = {"payload": {"adapter": "agent", "spec": {"runtime": {"tools": {"mcp": mcp}}}}}
    (payload_dir / "payload.yml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    return tmp_path


def _ws(auth_type: str) -> MagicMock:
    ws = MagicMock(name="ws")
    ws.config.auth_type = auth_type
    ws.config.client_id = "client"

    def _list(*, parent: str) -> list[McpService]:
        schema = parent.removeprefix("schemas/")
        return [McpService(name=f"mcp-services/{schema}.{name}") for name in ("gmail", "kafka")]

    ws.ai_gateway.list_mcp_services.side_effect = _list
    return ws


def _run(root: Path, ws: MagicMock, *extra: str) -> int:
    with patch("agent_foundry.workspace_client.prefer_workspace_client", return_value=ws):
        return cli.main(["--instance", "demo", "--project-root", str(root), *extra])


def test_as_sp_prints_policy_and_resolution(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _instance(tmp_path, {"source": "unity_gateway", "gateway": GATEWAY})

    assert _run(root, _ws("oauth-m2m"), "--as", "sp") == 0

    out = capsys.readouterr().out
    assert "(as sp)" in out
    assert "1. team.pinned.jira  identity=sp  origin=pinned  allow=search_*" in out
    assert "2. team.mcp.gmail  identity=sp  origin=discovered" in out
    assert "team.mine." not in out.split("Mounted")[1]


def test_as_current_user_lists_catalog(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _instance(tmp_path, {"source": "unity_gateway", "gateway": GATEWAY})

    assert _run(root, _ws("databricks-cli"), "--as-current-user") == 0

    out = capsys.readouterr().out
    assert "team.mine.kafka  identity=obo  origin=selected" in out
    assert "team.mine.gmail  origin=selected  reason=excluded" in out


def test_as_current_user_refuses_service_principal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _instance(tmp_path, {"source": "unity_gateway", "gateway": GATEWAY})

    assert _run(root, _ws("oauth-m2m"), "--as-current-user") == 1

    assert "--as-current-user needs user credentials" in capsys.readouterr().err


def test_workspace_source_has_nothing_to_resolve(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _instance(tmp_path, {"source": "workspace"})

    assert _run(root, _ws("databricks-cli")) == 1

    assert "nothing to resolve" in capsys.readouterr().err
