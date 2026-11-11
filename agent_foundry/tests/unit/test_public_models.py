"""Tests for the public typed return-value models (#159).

These tests guard the user-facing contract on
:class:`agent_foundry.DeployResult` and
:class:`agent_foundry.StateOutputs`.  Changes here are user-visible and
should be deliberate.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_foundry import DeployResult, StateOutputs
from agent_foundry.bricks import BrickResult

# ---------------------------------------------------------------------------
# DeployResult
# ---------------------------------------------------------------------------


class TestDeployResult:
    def test_construction_minimal(self):
        dr = DeployResult(instance_name="foo", target="dev", bricks={})
        assert dr.instance_name == "foo"
        assert dr.target == "dev"
        assert dr.bricks == {}

    def test_construction_with_brick_results(self):
        dr = DeployResult(
            instance_name="foo",
            target="dev",
            bricks={
                "Warehouse": [BrickResult(name="*", status="ok")],
                "Genie Spaces": [
                    BrickResult(name="info_schema", status="ok", rc=0),
                    BrickResult(name="other", status="skipped"),
                ],
            },
        )
        assert dr.bricks["Warehouse"][0].status == "ok"
        assert dr.bricks["Genie Spaces"][1].name == "other"
        assert len(dr.bricks["Genie Spaces"]) == 2

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            DeployResult.model_validate(
                {
                    "instance_name": "foo",
                    "target": "dev",
                    "bricks": {},
                    "rogue_field": "nope",
                }
            )

    def test_model_dump_includes_all_fields(self):
        dr = DeployResult(instance_name="foo", target="dev", bricks={})
        dumped = dr.model_dump()
        assert dumped == {
            "instance_name": "foo",
            "target": "dev",
            "bricks": {},
        }


# ---------------------------------------------------------------------------
# StateOutputs
# ---------------------------------------------------------------------------


class TestStateOutputs:
    def test_construction_with_variables(self):
        so = StateOutputs(
            variables={"warehouse_id": "wh-123", "genie_space_id__main": "sp-9"},
            bricks_loaded=["warehouse", "genie_spaces"],
        )
        assert so.variables["warehouse_id"] == "wh-123"
        assert so.bricks_loaded == ["warehouse", "genie_spaces"]

    def test_construction_empty(self):
        so = StateOutputs(variables={}, bricks_loaded=[])
        assert so.variables == {}
        assert so.bricks_loaded == []

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            StateOutputs.model_validate(
                {
                    "variables": {},
                    "bricks_loaded": [],
                    "extra": "nope",
                }
            )

    def test_back_compat_dict_update_through_variables(self):
        """Documented migration path for the 12+ pre-#159 call sites
        that did ``variables.update(load_state_outputs(...))``.
        """
        so = StateOutputs(
            variables={"a": "1", "b": "2"},
            bricks_loaded=["agent"],
        )
        target: dict[str, str] = {"existing": "x"}
        target.update(so.variables)
        assert target == {"existing": "x", "a": "1", "b": "2"}

    def test_iter_yields_field_pairs_not_inner_variables(self):
        """Pydantic v2's natural iter protocol yields *model fields*
        (variables, bricks_loaded), not the inner var key/value pairs.
        Document this so callers don't accidentally do
        ``dict.update(so)`` and merge the model itself rather than its
        inner variables.
        """
        so = StateOutputs(variables={"k": "v"}, bricks_loaded=["agent"])
        as_dict = dict(so)
        assert as_dict == {
            "variables": {"k": "v"},
            "bricks_loaded": ["agent"],
        }
        # The footgun this guards against:
        wrong_target: dict[str, object] = {}
        wrong_target.update(so)
        assert "k" not in wrong_target  # NOT the inner var key
        assert "variables" in wrong_target  # field name leaked in

    def test_model_dump_includes_all_fields(self):
        so = StateOutputs(variables={"k": "v"}, bricks_loaded=["agent"])
        assert so.model_dump() == {
            "variables": {"k": "v"},
            "bricks_loaded": ["agent"],
        }
