"""``agent_foundry.testing.live``: resolving the deployment a ``tests/live/`` check verifies."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from databricks.sdk.service.sql import StatementState

from agent_foundry.testing.live import (
    live_required,
    load_live_target,
    published_outputs,
    quote_identifier,
    run_sql,
)
from agent_foundry.testing.workspace import WorkspaceConfigError


@pytest.fixture
def binding_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("AGENT_FOUNDRY_TEST_CATALOG", "ci_cat")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi123")
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("true", True), ("", False), ("0", False)])
def test_live_required(monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool) -> None:
    monkeypatch.setenv("AGENT_FOUNDRY_REQUIRE_LIVE", raw)
    assert live_required() is expected


@pytest.mark.usefixtures("binding_env")
def test_resolves_the_single_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "instances" / "af-br-canary").mkdir(parents=True)
    monkeypatch.setenv("AGENT_FOUNDRY_PROJECT_ROOT", str(tmp_path))
    target = load_live_target()
    assert target.instance == "af-br-canary"
    assert target.instance_dir == tmp_path.resolve() / "instances" / "af-br-canary"
    assert target.binding.catalog == "ci_cat"


@pytest.mark.usefixtures("binding_env")
@pytest.mark.parametrize("instances", [(), ("a", "b")])
def test_needs_exactly_one_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instances: tuple[str, ...]
) -> None:
    (tmp_path / "instances").mkdir()
    for name in instances:
        (tmp_path / "instances" / name).mkdir()
    monkeypatch.setenv("AGENT_FOUNDRY_PROJECT_ROOT", str(tmp_path))
    with pytest.raises(WorkspaceConfigError, match="exactly one instance"):
        load_live_target()


def test_missing_binding_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    with pytest.raises(WorkspaceConfigError):
        load_live_target()


def test_published_outputs_keys_by_sub_instance_and_drops_empty() -> None:
    variables = {"job_id__nightly": "7", "job_id__stale": "", "job_ids": "x", "warehouse_id": "w"}
    assert published_outputs(variables, "job_id") == {"nightly": "7"}


def test_quote_identifier_escapes_backticks() -> None:
    assert quote_identifier("cat", "sch`ema", "t") == "`cat`.`sch``ema`.`t`"


def _response(state: StatementState, rows: list[list[str]] | None = None, error: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        statement_id="s-1",
        status=SimpleNamespace(state=state, error=SimpleNamespace(message=error) if error else None),
        result=SimpleNamespace(data_array=rows),
    )


class _Statements:
    def __init__(self, *responses: SimpleNamespace) -> None:
        self._responses = list(responses)
        self.executed: dict[str, Any] = {}

    def execute_statement(self, **kwargs: Any) -> SimpleNamespace:
        self.executed = kwargs
        return self._responses.pop(0)

    def get_statement(self, statement_id: str) -> SimpleNamespace:
        assert statement_id == "s-1"
        return self._responses.pop(0)


def test_run_sql_polls_until_done_and_binds_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_foundry.testing.live.time.sleep", lambda _s: None)
    statements = _Statements(_response(StatementState.PENDING), _response(StatementState.SUCCEEDED, [["1"]]))
    ws = SimpleNamespace(statement_execution=statements)
    assert run_sql(ws, "wh", "SELECT :x", parameters={"x": "1"}) == [["1"]]
    assert [(p.name, p.value) for p in statements.executed["parameters"]] == [("x", "1")]
    assert statements.executed["warehouse_id"] == "wh"


def test_run_sql_raises_with_the_server_error() -> None:
    ws = SimpleNamespace(statement_execution=_Statements(_response(StatementState.FAILED, error="TABLE_NOT_FOUND")))
    with pytest.raises(RuntimeError, match="TABLE_NOT_FOUND"):
        run_sql(ws, "wh", "SELECT * FROM missing")


def test_run_sql_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent_foundry.testing.live.time.sleep", lambda _s: None)
    ws = SimpleNamespace(statement_execution=_Statements(*[_response(StatementState.RUNNING)] * 3))
    with pytest.raises(TimeoutError):
        run_sql(ws, "wh", "SELECT 1", timeout_seconds=-1)
