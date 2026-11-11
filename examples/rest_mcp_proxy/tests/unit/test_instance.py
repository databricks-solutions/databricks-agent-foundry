"""Offline validation for the rest_mcp proxy instance.

Verifies the instance identity + the rest_mcp payload spec are well-formed
without a live workspace. Run with:

    python -m pytest instances/<name>/tests/unit -v
"""

from __future__ import annotations

from pathlib import Path


class TestInstanceFoundryToml:
    def test_has_required_identity_fields(self, instance_config: dict) -> None:
        assert "name" in instance_config
        assert "schema_prefix" in instance_config

    def test_name_matches_directory(self, instance_dir: Path, instance_config: dict) -> None:
        # ``agent-foundry init`` names the instance directory after the chosen
        # project and rewrites ``name`` to match.  Compare with hyphen/underscore
        # normalised so this holds both in the example source tree (package dir
        # uses ``_``) and in a rendered downstream repo.
        norm = lambda s: s.replace("-", "_")  # noqa: E731
        assert norm(instance_config["name"]) == norm(instance_dir.name)

    def test_app_name_derives_from_name(self, instance_config: dict) -> None:
        from agent_foundry import FoundryInstanceConfig

        cfg = FoundryInstanceConfig.model_validate(instance_config)
        assert cfg.app_name == cfg.name


class TestRestMcpPayload:
    def test_payload_manifest_exists(self, instance_dir: Path) -> None:
        assert (instance_dir / "payloads" / "rest_mcp" / "payload.yml").is_file()

    def test_spec_loads_and_validates(self, instance_dir: Path) -> None:
        from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

        spec = load_rest_mcp_spec(instance_dir)
        assert [api.name for api in spec.apis] == ["github"]

    def test_github_api_is_https_and_allowlisted(self, instance_dir: Path) -> None:
        from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

        github = load_rest_mcp_spec(instance_dir).apis[0]
        assert github.rest.base_url.startswith("https://")
        assert "api.github.com" in github.rest.allowed_hosts
        assert github.rest.auth.mode == "none"

    def test_tools_cover_declared_shapes(self, instance_dir: Path) -> None:
        from payloads.rest_mcp.runtime.config import load_rest_mcp_spec

        tools = {t.name: t for t in load_rest_mcp_spec(instance_dir).apis[0].tools}
        assert set(tools) == {"get_repository", "search_repositories"}
        assert tools["get_repository"].request.method == "GET"
        assert tools["get_repository"].response.project is not None
