"""Round-trip validation tests for the typed scaffolds.

The Pydantic-backed scaffolds (warehouse, evaluation, and the agent
payload manifest) are the authoritative discovery surface for those
configurable shapes.  If a scaffold drifts from its typed contract,
downstream authors silently get an invalid starter file.  These tests
parse each scaffold back through the model that owns the on-disk shape
so any drift fails CI.

The agent's runtime config is a payload-owned scaffold verb
(``agent-foundry scaffold payload`` -> ``payloads/agent/payload.yml``,
contributed by the agent adapter, #368), validated here against the
payload-owned :class:`payloads.agent.contracts.AgentPayloadFile`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent_foundry.scaffold import run_scaffold


@pytest.fixture
def instance_dir(tmp_path: Path) -> Path:
    inst = tmp_path / "instances" / "demo"
    inst.mkdir(parents=True)
    return inst


def test_warehouse_scaffold_validates_against_warehouse_yaml_shape(
    instance_dir: Path,
) -> None:
    """The scaffolded ``warehouse/config.yml`` must validate against
    :func:`agent_foundry.config.extract_warehouse_config` + the
    :class:`agent_foundry.config.WarehouseConfig` model.
    """
    from agent_foundry.config import WarehouseConfig, extract_warehouse_config

    run_scaffold("warehouse", instance_dir, force=False)
    raw = yaml.safe_load(
        (instance_dir / "warehouse" / "config.yml").read_text(encoding="utf-8"),
    )
    kwargs = extract_warehouse_config(raw)
    # If the scaffold introduces an unknown key, this raises -- the
    # model has ``frozen=True`` and Pydantic's default extra="ignore"
    # would let unknowns through silently, so ``extract_warehouse_config``
    # is the contract gate.
    cfg = WarehouseConfig(**kwargs)
    # The scaffold is an *opinionated starter*, intentionally distinct
    # from the conservative framework defaults in WarehouseConfig (those
    # remain backwards-compatible for instances that omit these keys).
    # The scaffold ships with light multi-cluster autoscaling so new
    # instances absorb concurrency spikes without an explicit edit.
    assert cfg.size == "X-Small"
    assert cfg.auto_stop_mins == 30
    assert cfg.min_num_clusters == 1
    assert cfg.max_num_clusters == 2


def test_agent_payload_scaffold_validates_against_payload_file(
    instance_dir: Path,
) -> None:
    """The scaffolded ``payloads/agent/payload.yml`` must validate
    against :class:`payloads.agent.contracts.AgentPayloadFile` -- the
    typed on-disk shape (``extra="forbid"``).
    """
    from payloads.agent.contracts import AgentPayloadFile

    run_scaffold("payload", instance_dir, force=False)
    raw = yaml.safe_load(
        (instance_dir / "payloads" / "agent" / "payload.yml").read_text(encoding="utf-8"),
    )
    cfg = AgentPayloadFile.model_validate(raw)
    assert cfg.payload.adapter == "agent"
    runtime = cfg.payload.spec.runtime
    assert runtime.model is not None
    assert (runtime.model.kind, runtime.model.name) == ("serving_endpoint", "databricks-claude-sonnet-4-5")
    assert runtime.max_tool_calls_per_turn == 5
    assert runtime.tools.mcp_only is True


def test_agent_payload_canary_patch_validates_against_payload_file(
    instance_dir: Path,
) -> None:
    """The ``agent_payload`` canary fixture's payload patch, merged into the
    scaffold, must still validate -- otherwise the canary fails at deploy.
    """
    from agent_foundry.testing.fixtures import apply_fixture, fixture_dir
    from payloads.agent.contracts import AgentPayloadFile

    run_scaffold("payload", instance_dir, force=False)
    apply_fixture(fixture_dir("agent_payload"), instance_dir)
    raw = yaml.safe_load(
        (instance_dir / "payloads" / "agent" / "payload.yml").read_text(encoding="utf-8"),
    )
    spec = AgentPayloadFile.model_validate(raw).payload.spec
    runtime = spec.runtime
    assert runtime.model is not None
    assert runtime.model.name == "databricks-claude-sonnet-5-5"
    assert runtime.reasoning.thinking == "adaptive"
    context = runtime.context
    assert spec.compaction_enabled()
    assert context.compaction_trigger_input_tokens() == 30_000
    assert context.clear_above_input_tokens() == 60_000
    assert context.prompt_cache.enabled is True
    assert runtime.tools.skills.loading != "lazy"
    assert spec.memory_enabled() and spec.plan_enabled()


def test_evaluation_scaffold_validates_against_evaluation_config(
    instance_dir: Path,
) -> None:
    """The scaffolded ``evaluation/eval_config.yml`` must round-trip
    through :class:`agent_foundry.config.EvaluationConfig` -- the
    typed on-disk shape.

    ``scorers`` is ``dict[str, list[str]]`` (semantically-named
    buckets like ``default``, ``with_ground_truth``, ``traces_only``,
    ``custom``); the older flat-list form is not valid against the
    runtime model.
    """
    from agent_foundry.config import EvaluationConfig

    run_scaffold("evaluation", instance_dir, force=False)
    raw = yaml.safe_load(
        (instance_dir / "evaluation" / "eval_config.yml").read_text(encoding="utf-8"),
    )
    cfg = EvaluationConfig.model_validate(raw)
    assert isinstance(cfg.scorers, dict)
    assert "default" in cfg.scorers
    assert cfg.max_traces == 100
    assert isinstance(cfg.production_monitoring, dict)
    smoke = yaml.safe_load(
        (instance_dir / "evaluation" / "datasets" / "smoke.yml").read_text(encoding="utf-8"),
    )
    assert len(smoke["dataset"]) == 1
    assert smoke["dataset"][0]["inputs"]["question"] == "Hello, how are you?"


@pytest.mark.parametrize(
    "scaffold_name",
    ["warehouse", "app", "evaluation", "payload"],
)
def test_pydantic_scaffolds_parse_as_yaml(
    instance_dir: Path,
    scaffold_name: str,
) -> None:
    """Every Pydantic-backed scaffold must be parseable YAML.  Trivial
    but catches accidental syntax errors in the inline templates.
    """
    run_scaffold(scaffold_name, instance_dir, force=False)
    for written in instance_dir.rglob("*.yml"):
        # yaml.safe_load returns None on empty input, dict otherwise --
        # either way, no exception means valid YAML.
        assert yaml.safe_load(written.read_text(encoding="utf-8")) is not None
