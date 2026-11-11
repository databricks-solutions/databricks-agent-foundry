"""``MlflowExperimentBrick.applies_to`` follows ``payload.requires`` (#454)."""

from __future__ import annotations

from pathlib import Path

import yaml

from agent_foundry._instance_layout import payload_root
from agent_foundry.bricks import DeployContext
from bricks.tracing.brick import MlflowExperimentBrick


def _ctx(instance_dir: Path) -> DeployContext:
    return DeployContext(instance_dir=instance_dir, target="dev")


def _write_payload(instance_dir: Path, adapter: str, requires: list[str]) -> None:
    root = payload_root(instance_dir, adapter)
    root.mkdir(parents=True)
    (root / "payload.yml").write_text(
        yaml.dump({"payload": {"adapter": adapter, "requires": requires, "spec": {}}}),
        encoding="utf-8",
    )


def test_applies_to_false_when_not_required(tmp_path: Path) -> None:
    _write_payload(tmp_path, "rest_mcp", [])
    assert MlflowExperimentBrick().applies_to(_ctx(tmp_path)) is False


def test_applies_to_true_when_required(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["tracing"])
    assert MlflowExperimentBrick().applies_to(_ctx(tmp_path)) is True


def test_applies_to_true_for_default_agent_without_manifest(tmp_path: Path) -> None:
    assert MlflowExperimentBrick().applies_to(_ctx(tmp_path)) is True
