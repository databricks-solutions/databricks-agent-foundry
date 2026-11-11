"""``ai_gateway/config.yml`` and ``applies_to``."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_foundry.bricks import DeployContext
from bricks.ai_gateway.brick import AiGatewayBrick
from bricks.ai_gateway.config import load_config
from bricks.ai_gateway.tests.unit.conftest import write_skill


def _write_config(instance_dir: Path, body: str) -> None:
    (instance_dir / "ai_gateway" / "config.yml").write_text(body, encoding="utf-8")


def _applies(instance_dir: Path) -> bool:
    ctx = DeployContext(instance_dir=instance_dir, target="dev", variables={}, dry_run=True)
    return AiGatewayBrick().applies_to(ctx)


def test_schema_is_substituted(instance_dir: Path) -> None:
    section = load_config(instance_dir, {"catalog": "main"}).skills
    assert section is not None and section.schema_fqn == "main.skills"


def test_unknown_section_is_rejected(instance_dir: Path) -> None:
    _write_config(instance_dir, "skills:\n  schema: a.b\nmcp_services: {}\n")
    with pytest.raises(ValidationError):
        load_config(instance_dir, {})


@pytest.mark.parametrize("schema", ["main", "a.b.c", "a..b", "${var.missing_ok}.b"])
def test_schema_must_be_catalog_dot_schema(instance_dir: Path, schema: str) -> None:
    _write_config(instance_dir, f"skills:\n  schema: '{schema}'\n")
    with pytest.raises((ValidationError, ValueError)):
        load_config(instance_dir, {})


def test_absent_file_means_no_sections(tmp_path: Path) -> None:
    assert load_config(tmp_path, {}).skills is None


def test_applies_only_with_section_and_skill_folders(instance_dir: Path) -> None:
    assert not _applies(instance_dir)
    write_skill(instance_dir, "triage")
    assert _applies(instance_dir)
    _write_config(instance_dir, "{}\n")
    assert not _applies(instance_dir)
