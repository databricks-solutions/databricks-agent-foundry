"""Tests for ``agent_foundry.destroy_base`` helpers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from databricks.sdk.errors import PermissionDenied

from agent_foundry.destroy_base import pick_any_running_warehouse, uc_object_exists
from agent_foundry.testing.fakes import FakeUcLookup


class _WarehousesStub:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def list(self) -> list[Any]:
        return self._items


class _WsStub:
    def __init__(self, items: list[Any]) -> None:
        self.warehouses = _WarehousesStub(items)


def _wh(wh_id: str, state: str) -> SimpleNamespace:
    return SimpleNamespace(id=wh_id, state=SimpleNamespace(value=state))


class TestPickAnyRunningWarehouse:
    """``pick_any_running_warehouse`` is a workspace primitive with no
    brick knowledge.  It must return only RUNNING warehouses and never
    auto-start a stopped one.
    """

    def test_returns_none_when_ws_is_none(self) -> None:
        assert pick_any_running_warehouse(None) is None

    def test_returns_first_running_warehouse(self) -> None:
        ws = _WsStub([_wh("wh-stopped", "STOPPED"), _wh("wh-running", "RUNNING")])
        assert pick_any_running_warehouse(ws) == "wh-running"

    def test_skips_non_running_warehouses(self) -> None:
        ws = _WsStub([_wh("wh-1", "STOPPED"), _wh("wh-2", "STARTING")])
        assert pick_any_running_warehouse(ws) is None

    def test_handles_plain_string_state(self) -> None:
        """Some SDK return shapes use a plain string instead of an enum."""
        ws = _WsStub([SimpleNamespace(id="wh-x", state="RUNNING")])
        assert pick_any_running_warehouse(ws) == "wh-x"

    def test_swallows_sdk_errors(self) -> None:
        class _Broken:
            warehouses = SimpleNamespace(list=lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        assert pick_any_running_warehouse(_Broken()) is None

    def test_empty_list_returns_none(self) -> None:
        assert pick_any_running_warehouse(_WsStub([])) is None


class _DeniedLookup:
    def get(self, full_name: str) -> None:
        raise PermissionDenied(f"no access to {full_name}")


class TestUcObjectExists:
    def test_existing_object(self) -> None:
        assert uc_object_exists(FakeUcLookup("cat.sch").get, "cat.sch") is True

    def test_not_found_object(self) -> None:
        assert uc_object_exists(FakeUcLookup().get, "cat.sch") is False

    def test_other_errors_count_as_existing(self) -> None:
        """A permission or transient error must not turn into a silent leak."""
        assert uc_object_exists(_DeniedLookup().get, "cat.sch") is True
