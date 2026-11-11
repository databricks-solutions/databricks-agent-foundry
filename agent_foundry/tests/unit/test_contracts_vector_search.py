"""Unit tests for ``agent_foundry.contracts.vector_search``."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from agent_foundry.contracts.vector_search import (
    DeltaSyncIndex,
    DirectAccessIndex,
    IndexConfig,
)


def _delta_sync_managed() -> dict[str, Any]:
    """A valid DELTA_SYNC config using Databricks-managed embeddings."""
    return {
        "endpoint": {"name": "ep"},
        "index": {
            "name": "a.b.c",
            "primary_key": "id",
            "index_type": "DELTA_SYNC",
            "delta_sync": {
                "source_table": "a.b.src",
                "embedding_source_columns": [
                    {"name": "content", "embedding_model_endpoint_name": "databricks-gte-large-en"},
                ],
            },
        },
    }


def _delta_sync_precomputed() -> dict[str, Any]:
    """A valid DELTA_SYNC config built from pre-computed embedding vectors."""
    return {
        "endpoint": {"name": "ep"},
        "index": {
            "name": "a.b.c",
            "primary_key": "id",
            "index_type": "DELTA_SYNC",
            "delta_sync": {
                "source_table": "a.b.src",
                "embedding_vector_columns": [{"name": "embedding", "embedding_dimension": 1024}],
            },
        },
    }


def _direct_access() -> dict[str, Any]:
    return {
        "endpoint": {"name": "ep"},
        "index": {
            "name": "a.b.c",
            "primary_key": "id",
            "index_type": "DIRECT_ACCESS",
            "direct_access": {
                "schema_json": '{"id": "int"}',
                "embedding_vector_columns": [{"name": "embedding", "embedding_dimension": 1024}],
            },
        },
    }


class TestValidConfigs:
    def test_delta_sync_managed_embeddings(self) -> None:
        cfg = IndexConfig.model_validate(_delta_sync_managed())
        assert isinstance(cfg.index, DeltaSyncIndex)
        assert cfg.endpoint.endpoint_type == "STANDARD"
        assert cfg.index.delta_sync.pipeline_type == "TRIGGERED"

    def test_delta_sync_precomputed_vectors(self) -> None:
        """Build-from-Delta-with-existing-embeddings path is accepted."""
        cfg = IndexConfig.model_validate(_delta_sync_precomputed())
        assert isinstance(cfg.index, DeltaSyncIndex)
        assert cfg.index.delta_sync.embedding_vector_columns[0].embedding_dimension == 1024
        assert cfg.index.delta_sync.embedding_source_columns == []

    def test_direct_access(self) -> None:
        cfg = IndexConfig.model_validate(_direct_access())
        assert isinstance(cfg.index, DirectAccessIndex)
        # ``schema_json`` is exposed under its YAML alias.
        assert cfg.index.direct_access.schema_json_ == '{"id": "int"}'


class TestEndpointType:
    def test_accepts_storage_optimized(self) -> None:
        raw = _delta_sync_managed()
        raw["endpoint"]["endpoint_type"] = "STORAGE_OPTIMIZED"
        cfg = IndexConfig.model_validate(raw)
        assert cfg.endpoint.endpoint_type == "STORAGE_OPTIMIZED"

    def test_rejects_serverless(self) -> None:
        """SERVERLESS is not a real Vector Search endpoint type."""
        raw = _delta_sync_managed()
        raw["endpoint"]["endpoint_type"] = "SERVERLESS"
        with pytest.raises(ValidationError, match="endpoint_type"):
            IndexConfig.model_validate(raw)


class TestEndpointOptions:
    def test_accepts_endpoint_options(self) -> None:
        raw = _delta_sync_managed()
        raw["endpoint"].update(
            {"budget_policy_id": "bp-1", "target_qps": 500, "usage_policy_id": "up-1"},
        )
        cfg = IndexConfig.model_validate(raw)
        assert cfg.endpoint.budget_policy_id == "bp-1"
        assert cfg.endpoint.target_qps == 500
        assert cfg.endpoint.usage_policy_id == "up-1"

    def test_rejects_non_positive_target_qps(self) -> None:
        raw = _delta_sync_managed()
        raw["endpoint"]["target_qps"] = 0
        with pytest.raises(ValidationError, match="target_qps"):
            IndexConfig.model_validate(raw)


class TestEnums:
    def test_rejects_bad_index_type(self) -> None:
        raw = _delta_sync_managed()
        raw["index"]["index_type"] = "DELTA-SYNC"
        with pytest.raises(ValidationError, match="index_type"):
            IndexConfig.model_validate(raw)

    def test_rejects_bad_pipeline_type(self) -> None:
        raw = _delta_sync_managed()
        raw["index"]["delta_sync"]["pipeline_type"] = "STREAMING"
        with pytest.raises(ValidationError, match="pipeline_type"):
            IndexConfig.model_validate(raw)


class TestEmbeddingExclusivity:
    def test_rejects_both_embedding_kinds(self) -> None:
        raw = _delta_sync_managed()
        raw["index"]["delta_sync"]["embedding_vector_columns"] = [
            {"name": "embedding", "embedding_dimension": 1024},
        ]
        with pytest.raises(ValidationError, match="mutually exclusive"):
            IndexConfig.model_validate(raw)

    def test_rejects_neither_embedding_kind(self) -> None:
        raw = _delta_sync_managed()
        raw["index"]["delta_sync"].pop("embedding_source_columns")
        with pytest.raises(ValidationError, match="exactly one"):
            IndexConfig.model_validate(raw)


class TestStructuralExclusivity:
    def test_rejects_delta_sync_with_direct_access_block(self) -> None:
        """extra='forbid' rejects a stray direct_access block on a
        DELTA_SYNC index -- this is the structural mutual exclusivity."""
        raw = _delta_sync_managed()
        raw["index"]["direct_access"] = {"schema_json": "{}"}
        with pytest.raises(ValidationError, match="direct_access"):
            IndexConfig.model_validate(raw)

    def test_rejects_typoed_key(self) -> None:
        raw = _delta_sync_managed()
        raw["index"]["delta_sync"]["source_tablee"] = "oops"
        with pytest.raises(ValidationError, match="source_tablee"):
            IndexConfig.model_validate(raw)


class TestRequiredFields:
    def test_missing_endpoint(self) -> None:
        raw = _delta_sync_managed()
        raw.pop("endpoint")
        with pytest.raises(ValidationError, match="endpoint"):
            IndexConfig.model_validate(raw)

    def test_missing_primary_key(self) -> None:
        raw = _delta_sync_managed()
        raw["index"].pop("primary_key")
        with pytest.raises(ValidationError, match="primary_key"):
            IndexConfig.model_validate(raw)

    def test_missing_index_type_fails_discrimination(self) -> None:
        raw = _delta_sync_managed()
        raw["index"].pop("index_type")
        with pytest.raises(ValidationError):
            IndexConfig.model_validate(raw)
