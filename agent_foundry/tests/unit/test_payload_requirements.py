"""Tests for :mod:`agent_foundry.payload_requirements`."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from agent_foundry._instance_layout import payload_root
from agent_foundry.contracts.payload import PayloadConfig
from agent_foundry.loader import FoundryConfigError
from agent_foundry.payload_requirements import (
    enforce_payload_requirements,
    payload_requires_brick,
)


def _write_payload(instance_dir: Path, adapter: str, requires: list[str]) -> None:
    root = payload_root(instance_dir, adapter)
    root.mkdir(parents=True)
    (root / "payload.yml").write_text(
        yaml.dump({"payload": {"adapter": adapter, "requires": requires, "spec": {}}}),
        encoding="utf-8",
    )


def test_unknown_requires_rejected() -> None:
    with pytest.raises(ValidationError, match="tracing"):
        PayloadConfig(adapter="agent", requires=["genie"])  # type: ignore[list-item]


def test_payload_requires_brick_reads_manifest(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["tracing"])
    assert payload_requires_brick(tmp_path, "tracing") is True
    assert payload_requires_brick(tmp_path, "observability") is False


def test_enforce_errors_when_required_brick_skipped(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["tracing"])
    with pytest.raises(FoundryConfigError, match="requires brick 'tracing'"):
        enforce_payload_requirements(
            tmp_path,
            {"tracing_experiment": "/Shared/exp"},
            skip_bricks={"tracing"},
        )


def test_enforce_allows_only_app_without_running_tracing(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["tracing"])
    enforce_payload_requirements(
        tmp_path,
        {},
        only_bricks={"app"},
    )


def test_enforce_errors_when_tracing_experiment_missing(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["tracing"])
    with pytest.raises(FoundryConfigError, match="tracing_experiment"):
        enforce_payload_requirements(tmp_path, {})


def test_enforce_ok_when_tracing_satisfied(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["tracing"])
    enforce_payload_requirements(tmp_path, {"tracing_experiment": "/Shared/exp"})


def test_enforce_errors_when_no_warehouse_resolvable(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["warehouse"])
    with pytest.raises(FoundryConfigError, match="scaffold warehouse"):
        enforce_payload_requirements(tmp_path, {})


def test_enforce_ok_with_dedicated_warehouse_config(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["warehouse"])
    (tmp_path / "warehouse").mkdir()
    (tmp_path / "warehouse" / "config.yml").write_text("warehouse: {}\n", encoding="utf-8")
    enforce_payload_requirements(tmp_path, {})


def test_enforce_ok_with_reused_warehouse_id(tmp_path: Path) -> None:
    _write_payload(tmp_path, "agent", ["warehouse"])
    enforce_payload_requirements(tmp_path, {"warehouse_id": "wh-1"})


def test_enforce_noop_when_nothing_required(tmp_path: Path) -> None:
    _write_payload(tmp_path, "rest_mcp", [])
    enforce_payload_requirements(tmp_path, {})
