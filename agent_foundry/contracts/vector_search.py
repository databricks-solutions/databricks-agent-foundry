"""Typed contract for ``instances/<inst>/vector_indexes/<name>/index_config.yml``.

These Pydantic models mirror the **raw YAML shape** of the Vector Search
brick's ``index_config.yml`` (issue #322).  The file was historically read
as untyped YAML and gated only by a hand-written validator; typing it makes
the deploy contract deterministic and gives editors ``$schema`` support, so
a misspelled enum (``index_type: DELTA-SYNC``) or an invalid block fails
fast at config-load instead of opaquely at deploy time.

The fields faithfully mirror the ``databricks-sdk`` create surface
(``WorkspaceClient.vector_search_indexes.create_index`` /
``vector_search_endpoints.create_endpoint``, SDK >= 0.109):

- ``endpoint_type`` is the closed ``STANDARD | STORAGE_OPTIMIZED`` set
  (``SERVERLESS`` is *not* a real Vector Search endpoint type).
- ``index`` is a discriminated union on ``index_type`` so the
  ``delta_sync`` and ``direct_access`` sub-blocks are mutually exclusive
  by construction.
- within a spec, ``embedding_source_columns`` (Databricks-managed
  embeddings) and ``embedding_vector_columns`` (pre-computed vectors) are
  mutually exclusive -- exactly one must be supplied.

Like :class:`agent_foundry.contracts.agent_app.AgentAppConfig`, every model
uses ``extra="forbid"``: unknown keys are rejected so a typo cannot slip
through silently.  ``forbid`` is also what enforces the
``delta_sync``/``direct_access`` exclusivity -- a ``DELTA_SYNC`` index
carrying a stray ``direct_access:`` block is rejected structurally.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class EndpointConfig(BaseModel):
    """The ``endpoint:`` block of ``index_config.yml``.

    Endpoints are shared across indexes; the deploy script creates the
    endpoint only if it does not already exist.

    ``endpoint_type`` maps to the SDK ``EndpointType`` enum.  ``target_qps``
    is a best-effort high-throughput target for ``STANDARD`` endpoints;
    ``budget_policy_id`` / ``usage_policy_id`` attach the endpoint to a
    Databricks budget / usage policy.  All three are optional and are only
    sent to the API when set.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    endpoint_type: Literal["STANDARD", "STORAGE_OPTIMIZED"] = "STANDARD"
    budget_policy_id: str | None = None
    target_qps: int | None = Field(default=None, gt=0)
    usage_policy_id: str | None = None


# ---------------------------------------------------------------------------
# Embedding columns
# ---------------------------------------------------------------------------


class EmbeddingSourceColumn(BaseModel):
    """A column whose text is embedded by a Databricks-managed model.

    Maps to the SDK ``EmbeddingSourceColumn``.  ``model_endpoint_name_for_query``
    optionally overrides the embedding model used at query time (ingestion
    still uses ``embedding_model_endpoint_name``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    embedding_model_endpoint_name: str
    model_endpoint_name_for_query: str | None = None


class EmbeddingVectorColumn(BaseModel):
    """A column that already contains pre-computed embedding vectors.

    Maps to the SDK ``EmbeddingVectorColumn``.  Used for the "build from a
    Delta table that already has embeddings" path (and for Direct Access
    indexes that store vectors directly).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    embedding_dimension: int = Field(gt=0)


class _EmbeddingColumnsSpec(BaseModel):
    """Shared base for the two index specs: the mutually-exclusive
    ``embedding_source_columns`` / ``embedding_vector_columns`` pair.

    Exactly one of the two must be populated: ``embedding_source_columns``
    selects Databricks-managed embeddings, ``embedding_vector_columns``
    selects pre-computed vectors.  Supplying both (or neither) is a config
    error caught here rather than at the API.
    """

    model_config = ConfigDict(extra="forbid")

    embedding_source_columns: list[EmbeddingSourceColumn] = Field(default_factory=list)
    embedding_vector_columns: list[EmbeddingVectorColumn] = Field(default_factory=list)

    @model_validator(mode="after")
    def _exactly_one_embedding_kind(self) -> "_EmbeddingColumnsSpec":
        has_source = bool(self.embedding_source_columns)
        has_vector = bool(self.embedding_vector_columns)
        if has_source and has_vector:
            raise ValueError(
                "embedding_source_columns and embedding_vector_columns are mutually "
                "exclusive -- supply exactly one (managed embeddings OR pre-computed vectors)",
            )
        if not has_source and not has_vector:
            raise ValueError(
                "exactly one of embedding_source_columns (managed embeddings) or "
                "embedding_vector_columns (pre-computed vectors) is required",
            )
        return self


# ---------------------------------------------------------------------------
# Index specs
# ---------------------------------------------------------------------------


class DeltaSyncSpec(_EmbeddingColumnsSpec):
    """The ``delta_sync:`` block (required when ``index_type=DELTA_SYNC``).

    Maps to the SDK ``DeltaSyncVectorIndexSpecRequest``.  The index
    auto-syncs from ``source_table``; ``pipeline_type`` controls triggered
    vs continuous sync, ``columns_to_sync`` narrows the synced columns
    (omit to sync all), and ``embedding_writeback_table`` optionally
    persists computed embeddings to a Delta table.
    """

    source_table: str
    pipeline_type: Literal["TRIGGERED", "CONTINUOUS"] = "TRIGGERED"
    columns_to_sync: list[str] | None = None
    embedding_writeback_table: str | None = None


class DirectAccessSpec(_EmbeddingColumnsSpec):
    """The ``direct_access:`` block (required when ``index_type=DIRECT_ACCESS``).

    Maps to the SDK ``DirectAccessVectorIndexSpec``.  The user manages CRUD
    via REST/SDK; ``schema_json`` declares the index schema.

    ``schema_json`` is exposed under an alias because the bare attribute
    name would shadow Pydantic's deprecated ``BaseModel.schema_json``
    classmethod (same pattern as ``ServerConfig.schema_``).
    ``populate_by_name=True`` keeps the on-disk YAML key ``schema_json``.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_json_: str | None = Field(default=None, alias="schema_json")


class DeltaSyncIndex(BaseModel):
    """A Delta Sync index: ``index_type: DELTA_SYNC`` + ``delta_sync:`` block."""

    model_config = ConfigDict(extra="forbid")

    index_type: Literal["DELTA_SYNC"] = "DELTA_SYNC"
    name: str
    primary_key: str
    delta_sync: DeltaSyncSpec


class DirectAccessIndex(BaseModel):
    """A Direct Access index: ``index_type: DIRECT_ACCESS`` + ``direct_access:`` block."""

    model_config = ConfigDict(extra="forbid")

    index_type: Literal["DIRECT_ACCESS"]
    name: str
    primary_key: str
    direct_access: DirectAccessSpec


# Discriminated union on ``index_type`` so the delta_sync / direct_access
# sub-blocks are mutually exclusive by construction.
IndexSpec = Annotated[
    Union[DeltaSyncIndex, DirectAccessIndex],
    Field(discriminator="index_type"),
]


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class IndexConfig(BaseModel):
    """The YAML shape of ``vector_indexes/<name>/index_config.yml``.

    ``tool_name`` is the stable LLM-facing identifier the skills assembler
    binds to this index (defaults to the directory name when omitted).
    """

    model_config = ConfigDict(extra="forbid")

    tool_name: str | None = None
    endpoint: EndpointConfig
    index: IndexSpec


__all__ = [
    "DeltaSyncIndex",
    "DeltaSyncSpec",
    "DirectAccessIndex",
    "DirectAccessSpec",
    "EmbeddingSourceColumn",
    "EmbeddingVectorColumn",
    "EndpointConfig",
    "IndexConfig",
    "IndexSpec",
]
