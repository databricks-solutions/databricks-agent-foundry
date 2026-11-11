"""Data domains of the deployed instance, resolved the way the deploy resolves them."""

from __future__ import annotations

import pytest

from agent_foundry.testing.live import LiveTarget
from bricks.data.brick import qualified_schema


def _domain_schemas(target: LiveTarget, variables: dict[str, str]) -> list[str]:
    domains_dir = target.instance_dir / "data_domains"
    subs = sorted(p for p in domains_dir.iterdir() if (p / "domain.yml").is_file()) if domains_dir.is_dir() else []
    return [qualified_schema(sub, variables) for sub in subs]


@pytest.fixture(scope="session")
def domain_schemas(live_target: LiveTarget, live_variables: dict[str, str]) -> list[str]:
    schemas = _domain_schemas(live_target, live_variables)
    assert schemas, f"no data domain under {live_target.instance_dir / 'data_domains'}"
    return schemas


@pytest.fixture(scope="session")
def gone_domain_schemas(live_target: LiveTarget, gone_variables: dict[str, str]) -> list[str]:
    schemas = _domain_schemas(live_target, gone_variables)
    assert schemas, f"no data domain under {live_target.instance_dir / 'data_domains'}"
    return schemas
