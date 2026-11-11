"""Tests for ``export-index``: an exported index must round-trip through the brick's own validator."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
import yaml
from databricks.sdk.service.vectorsearch import (
    DeltaSyncVectorIndexSpecResponse,
    DirectAccessVectorIndexSpec,
    EmbeddingSourceColumn,
    EmbeddingVectorColumn,
    EndpointInfo,
    EndpointType,
    PipelineType,
    VectorIndex,
    VectorIndexType,
)

from bricks.vector_search.scripts import export_index
from bricks.vector_search.scripts.validate_index import validate_index_config

_INDEX_NAME = "cat.sch.docs_index"

_DELTA_SYNC_INDEX = VectorIndex(
    name=_INDEX_NAME,
    endpoint_name="vs-endpoint",
    primary_key="id",
    index_type=VectorIndexType.DELTA_SYNC,
    delta_sync_index_spec=DeltaSyncVectorIndexSpecResponse(
        source_table="cat.sch.docs",
        pipeline_type=PipelineType.TRIGGERED,
        embedding_source_columns=[
            EmbeddingSourceColumn(name="content", embedding_model_endpoint_name="databricks-gte-large-en")
        ],
    ),
)

_DIRECT_ACCESS_INDEX = VectorIndex(
    name=_INDEX_NAME,
    endpoint_name="vs-endpoint",
    primary_key="id",
    index_type=VectorIndexType.DIRECT_ACCESS,
    direct_access_index_spec=DirectAccessVectorIndexSpec(
        schema_json='{"id": "int", "embedding": "array<float>"}',
        embedding_vector_columns=[EmbeddingVectorColumn(name="embedding", embedding_dimension=1024)],
    ),
)


def _client(index: VectorIndex, endpoint: EndpointInfo | Exception) -> MagicMock:
    client = MagicMock()
    client.vector_search_indexes.get_index.return_value = index
    if isinstance(endpoint, Exception):
        client.vector_search_endpoints.get_endpoint.side_effect = endpoint
    else:
        client.vector_search_endpoints.get_endpoint.return_value = endpoint
    return client


def _use_client(monkeypatch: pytest.MonkeyPatch, client: MagicMock) -> None:
    monkeypatch.setattr(export_index, "_get_client", lambda *args, **kwargs: client)


def _exported(output_dir: Path) -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load((output_dir / "index_config.yml").read_text(encoding="utf-8")))


def test_delta_sync_export_validates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_client(monkeypatch, _client(_DELTA_SYNC_INDEX, EndpointInfo(endpoint_type=EndpointType.STORAGE_OPTIMIZED)))

    assert export_index.main(["--index-name", _INDEX_NAME, "--output", str(tmp_path)]) == 0

    config = _exported(tmp_path)
    validate_index_config(config, source=tmp_path / "index_config.yml")
    assert config["endpoint"] == {"name": "vs-endpoint", "endpoint_type": "STORAGE_OPTIMIZED"}
    assert config["index"]["delta_sync"] == {
        "source_table": "cat.sch.docs",
        "pipeline_type": "TRIGGERED",
        "embedding_source_columns": [{"name": "content", "embedding_model_endpoint_name": "databricks-gte-large-en"}],
    }


def test_direct_access_export_validates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_client(monkeypatch, _client(_DIRECT_ACCESS_INDEX, EndpointInfo(endpoint_type=EndpointType.STANDARD)))

    export_index.export_index(_INDEX_NAME, tmp_path)

    config = _exported(tmp_path)
    validate_index_config(config, source=tmp_path / "index_config.yml")
    assert config["index"]["direct_access"]["embedding_vector_columns"] == [
        {"name": "embedding", "embedding_dimension": 1024}
    ]


def test_endpoint_lookup_failure_keeps_standard_and_existing_benchmarks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _use_client(monkeypatch, _client(_DELTA_SYNC_INDEX, RuntimeError("endpoint not visible")))
    queries = tmp_path / "benchmarks" / "queries.yml"
    queries.parent.mkdir(parents=True)
    queries.write_text("queries: [existing]\n", encoding="utf-8")

    export_index.export_index(_INDEX_NAME, tmp_path)

    assert _exported(tmp_path)["endpoint"]["endpoint_type"] == "STANDARD"
    assert queries.read_text(encoding="utf-8") == "queries: [existing]\n"


def test_creates_empty_benchmark_queries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _use_client(monkeypatch, _client(_DELTA_SYNC_INDEX, EndpointInfo(endpoint_type=EndpointType.STANDARD)))

    export_index.export_index(_INDEX_NAME, tmp_path)

    assert yaml.safe_load((tmp_path / "benchmarks" / "queries.yml").read_text(encoding="utf-8")) == {"queries": []}
