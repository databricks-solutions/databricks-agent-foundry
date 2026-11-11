"""Tests for ``payloads/agent/deploy/mcp/validate.py`` (issue #107)."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from payloads.agent.deploy.mcp.validate import validate_server_yml
from payloads.agent.tests.unit.deploy.mcp.conftest import write_server_yml

# ---------------------------------------------------------------------------
# Happy-path: each server type passes validation
# ---------------------------------------------------------------------------


class TestHappyPaths:
    """One canonical happy-path per server type covered by the spec."""

    def test_managed_functions(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "agent_functions",
            """
            type: managed_functions
            catalog: "${var.catalog}"
            schema: "${var.schema_prefix}_agent"
            tools:
              allow: ["lookup_status"]
            """,
        )
        config, errors, warnings = validate_server_yml(sub / "server.yml", mcp_variables)
        assert errors == []
        assert warnings == []
        assert config is not None
        assert config.type == "managed_functions"
        assert config.catalog == "test_catalog"
        assert config.schema_ == "test_app_agent"

    def test_managed_genie(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "sales_genie",
            """
            type: managed_genie
            space_id: "${var.sales_genie_space_id}"
            tools:
              allow: ["query_genie"]
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert errors == []
        assert config is not None and config.type == "managed_genie"
        assert config.space_id == "sgs_123"

    def test_managed_vector_search_preserves_all_ai_search_meta(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "docs_search",
            """
            type: managed_vector_search
            catalog: "${var.catalog}"
            schema: "${var.schema_prefix}_indexes"
            tools:
              allow: ["test_catalog__test_app_indexes__docs_index"]
              default_meta:
                columns: "id,content,doc_uri"
                columns_to_rerank: "content,title"
                filters: '{"category": "documentation"}'
                include_score: true
                num_results: 10
                query_type: "HYBRID"
                score_threshold: 0.5
            """,
        )
        config, errors, warnings = validate_server_yml(sub / "server.yml", mcp_variables)

        assert errors == []
        assert warnings == []
        assert config is not None and config.type == "managed_vector_search"
        assert config.tools.default_meta == {
            "columns": "id,content,doc_uri",
            "columns_to_rerank": "content,title",
            "filters": '{"category": "documentation"}',
            "include_score": True,
            "num_results": 10,
            "query_type": "HYBRID",
            "score_threshold": 0.5,
        }

    def test_external(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "github",
            """
            type: external
            connection_name: "github_mcp"
            tools:
              allow: ["search_repos", "get_pull_requests"]
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert errors == []
        assert config is not None and config.type == "external"
        assert config.connection_name == "github_mcp"

    def test_managed_sql_no_extra_fields(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "sql",
            """
            type: managed_sql
            tools:
              allow: ["execute_sql"]
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert errors == []
        assert config is not None and config.type == "managed_sql"

    def test_custom_app(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "my_mcp",
            """
            type: custom_app
            app_url: "https://my-mcp.workspace.databricksapps.com"
            tools:
              allow: ["do_thing"]
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert errors == []
        assert config is not None and config.type == "custom_app"


# ---------------------------------------------------------------------------
# Structural failures
# ---------------------------------------------------------------------------


class TestErrors:
    """Each negative case the spec calls out by name."""

    def test_missing_file(self, tmp_path: Path) -> None:
        config, errors, warnings = validate_server_yml(tmp_path / "server.yml", {})
        assert config is None
        assert any("not found" in e for e in errors)

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        bad = tmp_path / "server.yml"
        bad.write_text("type: managed_sql\n  bad: indent\n", encoding="utf-8")
        config, errors, _ = validate_server_yml(bad, {})
        assert config is None
        assert any("not valid YAML" in e for e in errors)

    def test_top_level_not_mapping(self, tmp_path: Path) -> None:
        bad = tmp_path / "server.yml"
        bad.write_text("- a\n- b\n", encoding="utf-8")
        config, errors, _ = validate_server_yml(bad, {})
        assert config is None
        assert any("must be a mapping" in e for e in errors)

    def test_empty_tools_allow(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow: []
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert any("tools.allow" in e for e in errors)

    def test_unknown_type(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: not_a_real_type
            tools:
              allow: ["do"]
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert errors  # exact message format is Pydantic-version-dependent

    @pytest.mark.parametrize(
        ("missing_field", "body"),
        [
            ("connection_name", "type: external\ntools:\n  allow: ['x']\n"),
            (
                "catalog",
                "type: managed_functions\nschema: s\ntools:\n  allow: ['x']\n",
            ),
            (
                "schema",
                "type: managed_functions\ncatalog: c\ntools:\n  allow: ['x']\n",
            ),
            ("space_id", "type: managed_genie\ntools:\n  allow: ['x']\n"),
            ("app_url", "type: custom_app\ntools:\n  allow: ['x']\n"),
        ],
    )
    def test_missing_per_type_field(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
        missing_field: str,
        body: str,
    ) -> None:
        sub = write_server_yml(mcp_instance_dir / "mcp_servers", "x", body)
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert any(missing_field in e for e in errors), errors

    def test_unresolved_variable(
        self,
        mcp_instance_dir: Path,
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_genie
            space_id: "${var.nonexistent}"
            tools:
              allow: ["q"]
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", {})
        assert config is None
        assert any("unresolved variable" in e for e in errors)

    def test_per_process_cache_unsupported(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow: ["q"]
            prompts:
              enabled: true
              allow: ["p"]
              cache: per_process
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert any("per_process" in e for e in errors)

    @pytest.mark.parametrize(
        "bad_prefix",
        ["NoUpper__", "starts_no_underscore", "missing_trailing_", "1leading_digit__"],
    )
    def test_bad_name_prefix(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
        bad_prefix: str,
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            dedent(
                f"""
                type: managed_sql
                tools:
                  allow: ["q"]
                  name_prefix: "{bad_prefix}"
                """,
            ),
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert any("name_prefix" in e for e in errors)


# ---------------------------------------------------------------------------
# Soft warnings
# ---------------------------------------------------------------------------


class TestWarnings:
    def test_prompts_enabled_empty_allow(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow: ["q"]
            prompts:
              enabled: true
              allow: []
            """,
        )
        config, errors, warnings = validate_server_yml(
            sub / "server.yml",
            mcp_variables,
        )
        assert errors == []
        assert config is not None
        assert any("prompts.allow" in w for w in warnings)

    def test_resources_enabled_empty_allow(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow: ["q"]
            resources:
              enabled: true
              allow: []
            """,
        )
        config, errors, warnings = validate_server_yml(
            sub / "server.yml",
            mcp_variables,
        )
        assert errors == []
        assert config is not None
        assert any("resources.allow" in w for w in warnings)

    def test_auto_poll_trigger_not_in_allow_warns(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        # ``managed_sql`` defaults auto_poll on; if the user prunes
        # tools.allow without disabling auto_poll, the trigger alias
        # vanishes and the wrapper silently no-ops.  The validator
        # should nudge the user before deploy so the misconfiguration
        # is caught at code-review time.
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow: ["something_unrelated"]
            """,
        )
        config, errors, warnings = validate_server_yml(
            sub / "server.yml",
            mcp_variables,
        )
        assert errors == []
        assert config is not None
        assert any("auto_poll.trigger" in w and "execute_sql_read_only" in w for w in warnings)

    def test_auto_poll_poll_with_not_in_allow_warns(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        # Trigger is allow-listed but the poll tool is not -- the
        # wrapper can dispatch the trigger but cannot resolve the
        # poll tool's upstream name, so auto-poll falls back to
        # returning the unpolled receipt.  Soft warning, not error.
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow: ["execute_sql_read_only"]
            """,
        )
        config, errors, warnings = validate_server_yml(
            sub / "server.yml",
            mcp_variables,
        )
        assert errors == []
        assert config is not None
        assert any("auto_poll.poll_with" in w and "poll_sql_result" in w for w in warnings)

    def test_auto_poll_disabled_skips_warnings(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        # ``auto_poll: false`` opts out entirely; no missing-trigger
        # warning should fire even when the trigger isn't in tools.allow.
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow: ["whatever"]
            auto_poll: false
            """,
        )
        config, errors, warnings = validate_server_yml(
            sub / "server.yml",
            mcp_variables,
        )
        assert errors == []
        assert config is not None
        assert not any("auto_poll" in w for w in warnings)


# ---------------------------------------------------------------------------
# Allow-list aliasing -- glob match + expose_as rename
# ---------------------------------------------------------------------------
#
# These tests cover the user-visible YAML surface introduced when we
# moved tool filtering from the SDK's exact-match ``tool_filter`` into
# the wrapper's ``list_tools``.  The contract validator has to:
#
# 1. Accept both legacy bare-string entries and dict-form entries with
#    ``match`` (+ optional ``expose_as``) in the same allow list.
# 2. Reject empty ``match`` strings (would match nothing or everything
#    depending on fnmatch's edge cases -- always a deployer mistake).
# 3. Reject ``expose_as`` values containing glob metacharacters
#    (literal-only -- aliases must be deterministic LLM-visible names).
# 4. Reject duplicate aliases across entries (the wrapper's reverse
#    map must stay injective; two upstream tools cannot collapse to
#    the same LLM-visible name).
#
# Failure cases assert that ``config is None`` (so the deploy script
# refuses to write state) and that the specific field name appears in
# at least one error string -- exact wording is Pydantic-version
# dependent, but the field path is stable.


class TestAllowEntryAliasing:
    """Parsing and validation of the str | ToolAllowEntry union."""

    def test_dict_form_with_glob_and_alias(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        # ``auto_poll: false`` here keeps this test focused on the
        # dict-form parsing of tools.allow.  Without it the
        # ``managed_genie`` default auto_poll fires a missing-poll
        # warning because the second entry omits ``expose_as`` (the
        # warning is correct -- exercised in TestWarnings instead).
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "genie",
            """
            type: managed_genie
            space_id: "${var.sales_genie_space_id}"
            tools:
              allow:
                - match: "query_space_*"
                  expose_as: query_space
                - match: "poll_response_*"
            auto_poll: false
            """,
        )
        config, errors, warnings = validate_server_yml(
            sub / "server.yml",
            mcp_variables,
        )
        assert errors == []
        assert warnings == []
        assert config is not None
        assert len(config.tools.allow) == 2
        # First entry: full mapping with expose_as.
        first = config.tools.allow[0]
        assert getattr(first, "match", None) == "query_space_*"
        assert getattr(first, "expose_as", None) == "query_space"
        # Second entry: dict form without expose_as is also valid.
        second = config.tools.allow[1]
        assert getattr(second, "match", None) == "poll_response_*"
        assert getattr(second, "expose_as", None) is None

    def test_mixed_str_and_dict_entries(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "sql",
            """
            type: managed_sql
            tools:
              allow:
                - execute_sql_read_only
                - match: "poll_*"
                  expose_as: poll
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert errors == []
        assert config is not None
        assert config.tools.allow[0] == "execute_sql_read_only"
        assert getattr(config.tools.allow[1], "match", None) == "poll_*"
        assert getattr(config.tools.allow[1], "expose_as", None) == "poll"

    def test_empty_match_rejected(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow:
                - match: ""
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert any("match" in e for e in errors), errors

    def test_expose_as_with_glob_rejected(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow:
                - match: "query_space_*"
                  expose_as: "query_*"
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert any("expose_as" in e for e in errors), errors

    def test_duplicate_alias_rejected_across_entries(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        # Two entries collapse to the same LLM-visible name "query".
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow:
                - match: "query_a_*"
                  expose_as: query
                - match: "query_b_*"
                  expose_as: query
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert any("duplicate alias" in e.lower() for e in errors), errors

    def test_duplicate_str_and_alias_rejected(
        self,
        mcp_instance_dir: Path,
        mcp_variables: dict[str, str],
    ) -> None:
        # Bare-string entries also participate in the alias-uniqueness
        # check (a bare string aliases to itself).
        sub = write_server_yml(
            mcp_instance_dir / "mcp_servers",
            "x",
            """
            type: managed_sql
            tools:
              allow:
                - foo
                - match: "foo_*"
                  expose_as: foo
            """,
        )
        config, errors, _ = validate_server_yml(sub / "server.yml", mcp_variables)
        assert config is None
        assert any("duplicate alias" in e.lower() for e in errors), errors
