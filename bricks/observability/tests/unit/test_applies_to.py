"""``ObservabilityBrick.applies_to`` follows ``payload.requires`` (#454)."""

from __future__ import annotations

from pathlib import Path

import yaml

from agent_foundry._instance_layout import payload_root
from agent_foundry.bricks import DeployContext
from bricks.observability.brick import ObservabilityBrick


def _write_payload(instance_dir: Path, adapter: str, requires: list[str]) -> None:
    root = payload_root(instance_dir, adapter)
    root.mkdir(parents=True)
    (root / "payload.yml").write_text(
        yaml.dump({"payload": {"adapter": adapter, "requires": requires, "spec": {}}}),
        encoding="utf-8",
    )


def test_applies_to_false_when_not_required(tmp_path: Path) -> None:
    _write_payload(tmp_path, "rest_mcp", [])
    ctx = DeployContext(instance_dir=tmp_path, target="dev")
    assert ObservabilityBrick().applies_to(ctx) is False


def test_applies_to_true_when_required(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["observability"])
    ctx = DeployContext(instance_dir=tmp_path, target="dev")
    assert ObservabilityBrick().applies_to(ctx) is True
