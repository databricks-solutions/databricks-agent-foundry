"""Offline tests for Vector Search index validation and config loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agent_foundry.contracts.vector_search import DeltaSyncIndex
from agent_foundry.loader import FoundryConfigError
from bricks.vector_search.scripts.validate_index import (
    load_config,
    substitute_variables,
    validate_index_config,
)

# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    def test_loads_valid_yaml(self, vs_instance_dir: Path) -> None:
        config = load_config(vs_instance_dir / "index_config.yml")
        assert "endpoint" in config
        assert "index" in config

    def test_returns_empty_for_empty_file(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yml"
        empty.write_text("")
        assert load_config(empty) == {}


# ---------------------------------------------------------------------------
# substitute_variables
# ---------------------------------------------------------------------------


class TestSubstituteVariables:
    def test_replaces_vars_in_nested_dicts(self) -> None:
        config = {
            "endpoint": {"name": "${var.ep}"},
            "index": {"name": "${var.cat}.${var.sch}.idx"},
        }
        result = substitute_variables(config, {"ep": "my-ep", "cat": "c", "sch": "s"})
        assert result["endpoint"]["name"] == "my-ep"
        assert result["index"]["name"] == "c.s.idx"

    def test_replaces_vars_in_lists(self) -> None:
        config = {"columns": ["${var.col1}", "fixed"]}
        result = substitute_variables(config, {"col1": "id"})
        assert result["columns"] == ["id", "fixed"]

    def test_leaves_non_string_values(self) -> None:
        config = {"count": 42, "flag": True}
        result = substitute_variables(config, {})
        assert result == {"count": 42, "flag": True}

    def test_unresolved_var_passthrough(self) -> None:
        config = {"name": "${var.missing}"}
        result = substitute_variables(config, {})
        assert result["name"] == "${var.missing}"


# ---------------------------------------------------------------------------
# validate_index_config -- Delta Sync
# ---------------------------------------------------------------------------


def _delta_sync_index(**delta_sync_overrides: Any) -> dict[str, Any]:
    delta_sync: dict[str, Any] = {
        "source_table": "t",
        "embedding_source_columns": [{"name": "c", "embedding_model_endpoint_name": "m"}],
    }
    delta_sync.update(delta_sync_overrides)
    return {
        "endpoint": {"name": "ep"},
        "index": {
            "name": "a.b.c",
            "primary_key": "id",
            "index_type": "DELTA_SYNC",
            "delta_sync": delta_sync,
        },
    }


class TestValidateDeltaSync:
    def test_valid_delta_sync(self, vs_instance_dir: Path, vs_variables: dict[str, str]) -> None:
        config = load_config(vs_instance_dir / "index_config.yml")
        config = substitute_variables(config, vs_variables)
        # Does not raise:
        cfg = validate_index_config(config)
        assert cfg.endpoint.name == "test-vs-endpoint"

    def test_missing_endpoint(self) -> None:
        config = {"index": {"name": "a.b.c", "primary_key": "id", "index_type": "DELTA_SYNC"}}
        with pytest.raises(FoundryConfigError, match="endpoint"):
            validate_index_config(config)

    def test_missing_primary_key(self) -> None:
        config = _delta_sync_index()
        config["index"].pop("primary_key")
        with pytest.raises(FoundryConfigError, match="primary_key"):
            validate_index_config(config)

    def test_invalid_index_type(self) -> None:
        config = {
            "endpoint": {"name": "ep"},
            "index": {"name": "a.b.c", "primary_key": "id", "index_type": "INVALID"},
        }
        with pytest.raises(FoundryConfigError, match="index_type"):
            validate_index_config(config)

    def test_missing_source_table(self) -> None:
        config = _delta_sync_index()
        config["index"]["delta_sync"].pop("source_table")
        with pytest.raises(FoundryConfigError, match="source_table"):
            validate_index_config(config)

    def test_missing_embedding_columns(self) -> None:
        config = _delta_sync_index()
        config["index"]["delta_sync"].pop("embedding_source_columns")
        with pytest.raises(FoundryConfigError, match="exactly one"):
            validate_index_config(config)

    def test_invalid_pipeline_type(self) -> None:
        config = _delta_sync_index(pipeline_type="INVALID")
        with pytest.raises(FoundryConfigError, match="pipeline_type"):
            validate_index_config(config)

    def test_embedding_source_missing_model(self) -> None:
        config = _delta_sync_index(embedding_source_columns=[{"name": "c"}])
        with pytest.raises(FoundryConfigError, match="embedding_model_endpoint_name"):
            validate_index_config(config)

    def test_precomputed_embeddings_ok(self) -> None:
        """DELTA_SYNC built from pre-computed embedding vectors validates."""
        config = _delta_sync_index()
        config["index"]["delta_sync"].pop("embedding_source_columns")
        config["index"]["delta_sync"]["embedding_vector_columns"] = [
            {"name": "embedding", "embedding_dimension": 1024},
        ]
        cfg = validate_index_config(config)
        assert isinstance(cfg.index, DeltaSyncIndex)
        assert cfg.index.delta_sync.embedding_vector_columns[0].embedding_dimension == 1024


# ---------------------------------------------------------------------------
# validate_index_config -- Direct Access
# ---------------------------------------------------------------------------


class TestValidateDirectAccess:
    def test_valid_direct_access(self) -> None:
        config = {
            "endpoint": {"name": "ep"},
            "index": {
                "name": "a.b.c",
                "primary_key": "id",
                "index_type": "DIRECT_ACCESS",
                "direct_access": {
                    "schema_json": '{"id": "int"}',
                    "embedding_vector_columns": [{"name": "emb", "embedding_dimension": 1024}],
                },
            },
        }
        # Does not raise:
        validate_index_config(config)

    def test_missing_direct_access_section(self) -> None:
        config = {
            "endpoint": {"name": "ep"},
            "index": {"name": "a.b.c", "primary_key": "id", "index_type": "DIRECT_ACCESS"},
        }
        with pytest.raises(FoundryConfigError, match="direct_access"):
            validate_index_config(config)

    def test_missing_embedding_in_direct_access(self) -> None:
        config = {
            "endpoint": {"name": "ep"},
            "index": {
                "name": "a.b.c",
                "primary_key": "id",
                "index_type": "DIRECT_ACCESS",
                "direct_access": {"schema_json": '{"id": "int"}'},
            },
        }
        with pytest.raises(FoundryConfigError, match="exactly one"):
            validate_index_config(config)

    def test_embedding_vector_missing_dimension(self) -> None:
        config = {
            "endpoint": {"name": "ep"},
            "index": {
                "name": "a.b.c",
                "primary_key": "id",
                "index_type": "DIRECT_ACCESS",
                "direct_access": {
                    "embedding_vector_columns": [{"name": "emb"}],
                },
            },
        }
        with pytest.raises(FoundryConfigError, match="embedding_dimension"):
            validate_index_config(config)


# ---------------------------------------------------------------------------
# Unresolved variables
# ---------------------------------------------------------------------------


class TestUnresolvedVars:
    def test_flags_unresolved_variables(self) -> None:
        config = _delta_sync_index()
        config["endpoint"]["name"] = "${var.missing_ep}"
        with pytest.raises(FoundryConfigError, match=r"\$\{var\.missing_ep\}"):
            validate_index_config(config)

    def test_no_flags_after_substitution(self, vs_instance_dir: Path, vs_variables: dict[str, str]) -> None:
        config = load_config(vs_instance_dir / "index_config.yml")
        config = substitute_variables(config, vs_variables)
        # Fully substituted scaffold validates cleanly.
        validate_index_config(config)


# ---------------------------------------------------------------------------
# Deploy dry-run
# ---------------------------------------------------------------------------


class TestDeployDryRun:
    def test_dry_run_succeeds(self, vs_instance_dir: Path, vs_variables: dict[str, str]) -> None:
        from bricks.vector_search.scripts.deploy_index import deploy

        rc = deploy(vs_instance_dir, "dev", vs_variables, dry_run=True)
        assert rc == 0

    def test_dry_run_fails_on_invalid_config(self, tmp_path: Path) -> None:
        from bricks.vector_search.scripts.deploy_index import deploy

        instance = tmp_path / "bad_instance"
        instance.mkdir(parents=True)
        (instance / "index_config.yml").write_text("index: {}")
        rc = deploy(instance, "dev", {}, dry_run=True)
        assert rc != 0
