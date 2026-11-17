"""Offline tool configuration checks for the MCP-first portfolio-assistant payload."""

from __future__ import annotations

from pathlib import Path


class TestToolConfig:
    def test_mcp_servers_present(self, instance_dir: Path):
        servers = instance_dir / "payloads" / "agent" / "mcp_servers"
        assert servers.is_dir(), "portfolio assistant is MCP-first; mcp_servers/ is required"
        names = {p.name for p in servers.iterdir() if p.is_dir()}
        assert any("docs" in n or "vector" in n or "search" in n for n in names)

    def test_genie_one_runs_as_user(self, instance_dir: Path):
        import yaml

        payload = yaml.safe_load((instance_dir / "payloads" / "agent" / "payload.yml").read_text(encoding="utf-8"))
        pinned = payload["payload"]["spec"]["runtime"]["tools"]["mcp"]["gateway"]["pinned"]
        assert {"fqn": "system.ai.genie_one_mcp", "identity": "obo"} in pinned

    def test_payload_enables_mcp_and_a2a(self, instance_dir: Path):
        import yaml

        payload = yaml.safe_load((instance_dir / "payloads" / "agent" / "payload.yml").read_text(encoding="utf-8"))
        spec = payload["payload"]["spec"]
        assert spec["integrations"]["mcp_server"]["enabled"] is True
        assert spec["integrations"]["a2a"]["enabled"] is True
