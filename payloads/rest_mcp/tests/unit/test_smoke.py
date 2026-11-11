"""rest_mcp smoke probes: one gateway probe per API, declared tools must be listed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_foundry.smoke import SmokeFailure, SmokeTarget
from payloads.rest_mcp import smoke

_PAYLOAD = """\
payload:
  adapter: rest_mcp
  spec:
    apis:
      - name: github
        rest:
          base_url: "https://api.github.com"
          allowed_hosts: ["api.github.com"]
          auth: { mode: none }
        tools:
          - name: get_repository
            description: "Fetch a repository."
            request: { method: GET, path: "/repos/{owner}/{repo}", args: [
              { name: owner, in: path, type: string, required: true },
              { name: repo, in: path, type: string, required: true } ] }
"""


def _target(tmp_path: Path) -> SmokeTarget:
    instance_dir = tmp_path / "my-proxy"
    (instance_dir / "payloads" / "rest_mcp").mkdir(parents=True)
    (instance_dir / "payloads" / "rest_mcp" / "payload.yml").write_text(_PAYLOAD, encoding="utf-8")
    return SmokeTarget(
        ws=object(), app_url="https://app", workspace_host="https://ws.example", instance_dir=instance_dir
    )


def test_one_probe_per_api_through_its_connection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    urls: list[str] = []

    def _list(_ws: object, url: str) -> list[object]:
        urls.append(url)
        return [SimpleNamespace(name="get_repository")]

    monkeypatch.setattr("agent_foundry.testing.mcp_session.list_mcp_tools", _list)
    (probe,) = smoke.rest_mcp_smoke_probes(_target(tmp_path))
    assert probe.name == "mcp gateway github"
    assert probe.run() == "get_repository"
    assert urls == ["https://ws.example/api/2.0/mcp/external/my-proxy_github_mcp"]


def test_missing_declared_tool_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("agent_foundry.testing.mcp_session.list_mcp_tools", lambda _ws, _url: [])
    (probe,) = smoke.rest_mcp_smoke_probes(_target(tmp_path))
    with pytest.raises(SmokeFailure, match="get_repository"):
        probe.run()
