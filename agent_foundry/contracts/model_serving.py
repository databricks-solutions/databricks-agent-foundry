"""Typed contract for ``instances/<inst>/model_serving/<name>/endpoint_config.yml``.

One serverless Model Serving endpoint serving one Unity Catalog model
version.  The version is pinned (``version``), resolved from an alias
(``alias``), the latest registered version (neither), or produced by an
optional ``build`` that runs a Python script as a serverless job.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

WorkloadType = Literal[
    "CPU",
    "CPU_MEDIUM",
    "CPU_LARGE",
    "GPU_SMALL",
    "GPU_MEDIUM",
    "GPU_LARGE",
    "MULTIGPU_MEDIUM",
]
WorkloadSize = Literal["Small", "Medium", "Large"]

_ENDPOINT_NAME = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$"
_UC_MODEL_NAME = r"^[^.\s]+\.[^.\s]+\.[^.\s]+$"
_LOCAL_PATH = r"^\./"


class ServedModelConfig(BaseModel):
    """The UC model the endpoint serves and the compute it runs on."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=_UC_MODEL_NAME, description="Three-level UC model name: catalog.schema.model.")
    version: str | None = Field(default=None, pattern=r"^[1-9][0-9]*$")
    alias: str | None = Field(default=None, min_length=1)
    workload_type: WorkloadType = "CPU"
    workload_size: WorkloadSize = "Small"
    scale_to_zero: bool = True
    environment_vars: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_version_selector(self) -> ServedModelConfig:
        if self.version and self.alias:
            raise ValueError("set at most one of model.version and model.alias")
        return self


class ModelBuildConfig(BaseModel):
    """Serverless job that logs and registers a new version of ``model.name``.

    The script receives ``--model-name`` and ``--experiment`` and must
    register exactly one new version.  It reruns when the script, a
    listed file, or a dependency changes.
    """

    model_config = ConfigDict(extra="forbid")

    script: str = Field(pattern=_LOCAL_PATH, description="Python file relative to the sub-instance (./...).")
    files: list[str] = Field(default_factory=list, description="Extra ./ files uploaded next to the script.")
    dependencies: list[str] = Field(default_factory=list, description="pip requirements of the serverless job.")
    environment_version: str = Field(default="3", description="Serverless environment version (sets Python).")
    timeout_minutes: int = Field(default=60, ge=5, le=240)

    @model_validator(mode="after")
    def _local_files(self) -> ModelBuildConfig:
        bad = [path for path in self.files if not path.startswith("./")]
        if bad:
            raise ValueError(f"build.files must be ./-relative paths: {bad}")
        return self


class ServingEndpointConfig(BaseModel):
    """Root of ``endpoint_config.yml``."""

    model_config = ConfigDict(extra="forbid")

    endpoint_name: str = Field(pattern=_ENDPOINT_NAME)
    model: ServedModelConfig
    build: ModelBuildConfig | None = None
    tags: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _build_owns_version(self) -> ServingEndpointConfig:
        if self.build and (self.model.version or self.model.alias):
            raise ValueError("model.version and model.alias cannot be combined with build")
        return self
