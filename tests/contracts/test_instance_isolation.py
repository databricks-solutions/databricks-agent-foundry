"""Instance names must produce disjoint provisioned identities."""

from __future__ import annotations

from pathlib import Path

import yaml

from agent_foundry.config import InstanceConfig, TargetConfig
from agent_foundry.testing.identity import make_run_identity
from agent_foundry.testing.scenarios import load_scenarios
from agent_foundry.variables import substitute

REPO = Path(__file__).resolve().parents[2]


def _cfg(*, name: str, schema_prefix: str, target_name: str, catalog: str = "main") -> InstanceConfig:
    return InstanceConfig(
        name=name,
        schema_prefix=schema_prefix,
        vs_endpoint_name="vs_docs_demo",
        target_name=target_name,
        target=TargetConfig(catalog=catalog),
    )


def _identities(cfg: InstanceConfig) -> dict[str, str]:
    variables = cfg.to_variables()
    job = substitute("${var.target_name}-${var.app_name}-example-job", variables)
    return {
        "app": cfg.app_resource_name,
        "warehouse": cfg.warehouse_display_name,
        "dashboard": cfg.dashboard_display_name,
        "solution_root": cfg.solution_root,
        "experiment": cfg.tracing_experiment,
        "agent_schema": cfg.agent_schema,
        "lakebase": f"{cfg.target_name}-{cfg.app_name}-lakebase",
        "job": job,
        "data_schema": substitute("${var.schema_prefix}", variables),
    }


def test_two_instance_names_do_not_share_identities() -> None:
    left = _identities(_cfg(name="alpha", schema_prefix="alpha", target_name="dev"))
    right = _identities(_cfg(name="beta", schema_prefix="beta", target_name="dev"))
    for key, value in left.items():
        assert value != right[key], key
    assert (
        _cfg(name="alpha", schema_prefix="alpha", target_name="dev").vs_endpoint_name
        == _cfg(name="beta", schema_prefix="beta", target_name="dev").vs_endpoint_name
    )


def test_two_targets_do_not_share_api_names() -> None:
    """Same instance, same catalog: Apps, jobs, warehouses, and paths still differ."""
    dev = _identities(_cfg(name="alpha", schema_prefix="alpha", target_name="dev"))
    prod = _identities(_cfg(name="alpha", schema_prefix="alpha", target_name="prod"))
    for key in ("app", "warehouse", "dashboard", "solution_root", "experiment", "lakebase", "job"):
        assert dev[key] != prod[key], key
    assert dev["data_schema"] == prod["data_schema"] == "alpha"
    assert dev["agent_schema"] == prod["agent_schema"]


def test_shipped_templates_namespace_schema_and_jobs() -> None:
    domain = yaml.safe_load((REPO / "bricks/data/_scaffold/domain.yml").read_text(encoding="utf-8"))
    assert domain["domain"]["schema"] == "${var.schema_prefix}"
    jobs = [
        REPO / "bricks/job/_scaffold/job.yml",
        REPO / "examples/portfolio_assistant/jobs/docs_ingest/job.yml",
        REPO / "examples/portfolio_assistant/jobs/data_universe_profile/job.yml",
    ]
    for path in jobs:
        name = yaml.safe_load(path.read_text(encoding="utf-8"))["name"]
        assert name.startswith("${var.target_name}-"), path


def test_default_canary_identities_are_disjoint() -> None:
    ids = [
        make_run_identity(scenario, "canary")
        for scenario in load_scenarios()
        if scenario.id in {"bricks", "agent_payload", "portfolio_assistant"}
    ]
    projects = [item.project_name for item in ids]
    prefixes = [item.schema_prefix for item in ids]
    assert projects == ["af-br-canary", "af-ag-canary", "af-pa-canary"]
    assert len(set(projects)) == len(projects)
    assert len(set(prefixes)) == len(prefixes)
