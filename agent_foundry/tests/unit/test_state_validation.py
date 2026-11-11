"""Tests for the state_schema contract validator (issue #156).

The validator lives in ``agent_foundry.state.validate_state``, is wired
into :class:`agent_foundry.bricks.StandardLayoutBrick.load_state_outputs`,
and is the only thing protecting downstream bricks from missing state
keys after #156.

Pilot brick: ``warehouse`` is the first to declare ``state_schema =
WarehouseState``.  These tests cover both the standalone validator and
the warehouse-brick integration end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from agent_foundry import BrickStateValidationError, load_state_outputs
from agent_foundry.bricks import DeployContext
from agent_foundry.state import save_state, validate_state
from agent_foundry.testing.fakes import FakeWorkspaceClient
from bricks.warehouse.brick import SqlWarehouseBrick, WarehouseState

# ---------------------------------------------------------------------------
# Validator unit tests
# ---------------------------------------------------------------------------


class _SampleSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")
    required_id: str
    optional_label: str = ""


class TestValidateState:
    def test_none_schema_is_noop(self):
        # The pre-#156 default: schema=None means anything goes.
        validate_state("anybrick", {"anything": "goes"}, None)

    def test_valid_state_passes(self):
        validate_state("brick_a", {"required_id": "abc"}, _SampleSchema)

    def test_extra_keys_tolerated(self):
        # Schema policy is extra="ignore" so optional / framework
        # metadata (e.g. workspace _audit trails) don't break validation.
        validate_state(
            "brick_a",
            {"required_id": "abc", "_audit": [{"who": "ci"}], "future": "field"},
            _SampleSchema,
        )

    def test_missing_required_field_raises_with_brick_name(self):
        with pytest.raises(BrickStateValidationError) as exc:
            validate_state("brick_a", {}, _SampleSchema)
        assert "brick_a" in str(exc.value)
        assert "required_id" in str(exc.value)
        assert "_SampleSchema" in str(exc.value)

    def test_missing_required_field_chains_pydantic_detail(self):
        with pytest.raises(BrickStateValidationError) as exc:
            validate_state("brick_a", {}, _SampleSchema)
        # The original pydantic ValidationError is preserved on __cause__
        # so debuggers / logs can drill in.
        from pydantic import ValidationError

        assert isinstance(exc.value.__cause__, ValidationError)

    def test_sub_instance_appears_in_error_message(self):
        with pytest.raises(BrickStateValidationError) as exc:
            validate_state(
                "brick_a",
                {},
                _SampleSchema,
                sub_instance="my_sub",
            )
        # Both brick name and sub-instance show up so operators can
        # pinpoint the offending file in nested-layout bricks.
        assert "brick_a" in str(exc.value)
        assert "my_sub" in str(exc.value)

    def test_wrong_type_raises(self):
        # Pydantic's coercion is intentionally strict here: a list is
        # not a valid str, so this fails fast.
        with pytest.raises(BrickStateValidationError):
            validate_state(
                "brick_a",
                {"required_id": ["not", "a", "string"]},
                _SampleSchema,
            )

    def test_subclass_of_value_error(self):
        # Downstream consumers may catch ValueError generically.
        assert issubclass(BrickStateValidationError, ValueError)


# ---------------------------------------------------------------------------
# Warehouse pilot integration tests
# ---------------------------------------------------------------------------
#
# These tests exercise the full read path (workspace state ->
# validate_state -> StandardLayoutBrick.load_state_outputs -> outputs)
# end-to-end via the public load_state_outputs aggregator.


def _seed_state(
    ws: FakeWorkspaceClient,
    inst: Path,
    brick_type: str,
    payload: dict,
    *,
    sub: str | None = None,
    authoring_dir: Path | None = None,
) -> None:
    """Write *payload* as the ``dev`` workspace state, creating *authoring_dir* (or *inst*)."""
    (authoring_dir or inst).mkdir(parents=True, exist_ok=True)
    save_state(ws, inst.name, brick_type, sub or inst.name, "dev", payload)


class TestWarehousePilot:
    def test_warehouse_state_schema_is_declared(self):
        # Tripwire: PR #156 must wire WarehouseState onto the brick.
        # If this asserts to None the pilot regressed.
        assert SqlWarehouseBrick.state_schema is WarehouseState

    def test_valid_warehouse_state_loads_cleanly(self, tmp_path: Path):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "warehouse",
            authoring_dir=inst / "warehouse",
            payload={
                "warehouse_id": "wh-abc",
                "name": "ml-warehouse",
            },
        )
        outputs = load_state_outputs(inst, "dev", ws=ws)
        assert outputs.variables.get("warehouse_id") == "wh-abc"
        assert "warehouse" in outputs.bricks_loaded

    def test_invalid_warehouse_state_raises_with_clear_message(self, tmp_path: Path):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        # Write state missing the required ``name`` field.  This is the
        # legacy pre-#156 shape; deploys against an instance with an old
        # state file should fail loud at the framework boundary.
        _seed_state(ws, inst, "warehouse", authoring_dir=inst / "warehouse", payload={"warehouse_id": "wh-abc"})

        with pytest.raises(BrickStateValidationError) as exc:
            load_state_outputs(inst, "dev", ws=ws)
        msg = str(exc.value)
        assert "warehouse" in msg
        assert "name" in msg
        assert "WarehouseState" in msg

    def test_extra_keys_in_warehouse_state_are_tolerated(self, tmp_path: Path):
        # Future-version metadata or workspace ``_audit`` trails must
        # not break the validator.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "warehouse",
            authoring_dir=inst / "warehouse",
            payload={
                "warehouse_id": "wh-abc",
                "name": "ml-warehouse",
                "_audit": [{"who": "ci", "when": "now"}],
                "future_field": "ignored",
            },
        )
        outputs = load_state_outputs(inst, "dev", ws=ws)
        assert outputs.variables["warehouse_id"] == "wh-abc"

    def test_empty_warehouse_state_skips_validation(self, tmp_path: Path):
        # An empty state file ({}) is the post-destroy convention --
        # equivalent to "no state yet", not a contract violation.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(ws, inst, "warehouse", authoring_dir=inst / "warehouse", payload={})

        # Must not raise; the variable simply isn't published.
        outputs = load_state_outputs(inst, "dev", ws=ws)
        assert "warehouse_id" not in outputs.variables

    def test_missing_state_file_skips_validation(self, tmp_path: Path):
        # Brick has never been deployed; no state file exists.
        inst = tmp_path / "app"
        (inst / "warehouse").mkdir(parents=True)

        outputs = load_state_outputs(inst, "dev")
        assert "warehouse_id" not in outputs.variables

    def test_brick_load_state_outputs_validates_directly(self, tmp_path: Path):
        # Confirms the validation hook lives in
        # StandardLayoutBrick.load_state_outputs, not just in the
        # aggregator -- so callers using the brick API directly also
        # get protection.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(ws, inst, "warehouse", authoring_dir=inst / "warehouse", payload={"warehouse_id": "wh-only"})

        ctx = DeployContext(instance_dir=inst, target="dev", ws=ws)
        with pytest.raises(BrickStateValidationError):
            SqlWarehouseBrick().load_state_outputs(ctx)


# ---------------------------------------------------------------------------
# Bundle: per-brick schema declarations (#172, #173, #174, #175)
# ---------------------------------------------------------------------------
#
# Tripwires + minimal happy/unhappy path.  The full integration mechanics
# (extra keys, empty state, missing file, direct brick API) are already
# covered exhaustively by ``TestWarehousePilot`` and exercise the same
# ``StandardLayoutBrick.load_state_outputs`` code path -- repeating them
# here would only add maintenance cost.


def _patch_tracing_state(monkeypatch: pytest.MonkeyPatch, state: dict[str, str]) -> None:
    """Make ``agent_foundry.state.load_state`` return *state* for the
    ``tracing`` brick only (#338: tracing state is workspace-backed, not
    a local file).  Other bricks keep reading empty state so the
    aggregator only exercises the brick under test.

    ``load_state`` is called positionally as ``(ws, instance_name,
    brick_type, ...)``, so index 2 is the brick type.
    """
    import agent_foundry.state as state_mod

    def fake_load_state(*args: object, **kwargs: object) -> dict[str, str]:
        brick_type = args[2] if len(args) > 2 else kwargs.get("brick_type")
        return dict(state) if brick_type == "tracing" else {}

    monkeypatch.setattr(state_mod, "load_state", fake_load_state)


class TestTracingMlflowExperimentSchema:
    def test_schema_declared(self):
        from bricks.tracing.brick import (
            MlflowExperimentBrick,
            MlflowExperimentState,
        )

        assert MlflowExperimentBrick.state_schema is MlflowExperimentState

    def test_valid_state_loads(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        inst = tmp_path / "app"
        inst.mkdir()
        _patch_tracing_state(monkeypatch, {"experiment_id": "exp-123"})
        outputs = load_state_outputs(inst, "dev")
        assert outputs.variables.get("experiment_id") == "exp-123"

    def test_missing_required_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        inst = tmp_path / "app"
        inst.mkdir()
        _patch_tracing_state(monkeypatch, {"unrelated": "x"})
        with pytest.raises(BrickStateValidationError) as exc:
            load_state_outputs(inst, "dev")
        assert "tracing" in str(exc.value)
        assert "experiment_id" in str(exc.value)


class TestGenieSpacesSchema:
    def test_schema_declared(self):
        from bricks.genie.brick import GenieSpacesBrick, GenieSpaceState

        assert GenieSpacesBrick.state_schema is GenieSpaceState

    def test_valid_state_loads_for_sub_instance(self, tmp_path: Path):
        # Nested layout: one workspace state per sub-instance, exposed
        # as ``genie_space_id__<sub>``.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "genie_spaces",
            {
                "space_id": "genie-abc",
                "title": "Trading Genie",
                # _audit trail comes from workspace-backed save_state;
                # must be tolerated.
                "_audit": [{"who": "ci"}],
            },
            sub="trading",
            authoring_dir=inst / "genie_spaces" / "trading",
        )
        outputs = load_state_outputs(inst, "dev", ws=ws)
        assert outputs.variables.get("genie_space_id__trading") == "genie-abc"

    def test_missing_required_raises_with_sub_instance(self, tmp_path: Path):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "genie_spaces",
            {"space_id": "genie-abc"},
            sub="trading",
            authoring_dir=inst / "genie_spaces" / "trading",
        )
        with pytest.raises(BrickStateValidationError) as exc:
            load_state_outputs(inst, "dev", ws=ws)
        msg = str(exc.value)
        assert "genie_spaces" in msg
        assert "trading" in msg
        assert "title" in msg


class TestVectorIndexesSchema:
    def test_schema_declared(self):
        from bricks.vector_search.brick import VectorIndexesBrick, VectorIndexState

        assert VectorIndexesBrick.state_schema is VectorIndexState

    def test_valid_state_loads_for_sub_instance(self, tmp_path: Path):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "vector_indexes",
            {
                "index_name": "main.demo.docs_idx",
                "endpoint_name": "shared-ep",
                "index_type": "DELTA_SYNC",
            },
            sub="docs",
            authoring_dir=inst / "vector_indexes" / "docs",
        )
        outputs = load_state_outputs(inst, "dev", ws=ws)
        assert outputs.variables.get("vs_index_name__docs") == "main.demo.docs_idx"
        assert outputs.variables.get("vs_endpoint_name__docs") == "shared-ep"

    def test_missing_required_raises(self, tmp_path: Path):
        # Pre-#174 state files only had ``index_name`` -- without
        # ``endpoint_name``/``index_type`` validation must fail loud
        # rather than silently producing partial outputs.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "vector_indexes",
            {"index_name": "main.demo.docs_idx"},
            sub="docs",
            authoring_dir=inst / "vector_indexes" / "docs",
        )
        with pytest.raises(BrickStateValidationError) as exc:
            load_state_outputs(inst, "dev", ws=ws)
        msg = str(exc.value)
        assert "vector_indexes" in msg
        assert "endpoint_name" in msg or "index_type" in msg


class TestLakebaseSchema:
    """Lakebase state is now owned by the Agent App brick (chat history
    is a capability of the App, not a peer brick).  These tests
    continue to validate the state contract introduced by issue #175,
    which lives in the shared workspace state.  Validation surfaces through the
    agent payload's chat-history loader which validates against
    :class:`payloads.agent.deploy.lakebase.LakebaseState`.
    """

    def test_schema_declared_on_lakebase_module(self):
        """The Lakebase state contract lives next to the provisioner in
        the agent payload; the payload loads/validates it."""
        from payloads.agent.deploy.lakebase import LakebaseState

        assert "lakebase_project_name" in LakebaseState.model_fields
        assert "lakebase_branch_path" in LakebaseState.model_fields
        assert "lakebase_database_path" in LakebaseState.model_fields

    def test_valid_state_loads(self, tmp_path: Path):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "lakebase",
            {
                "lakebase_project_name": "demo-proj",
                "lakebase_branch_path": "main",
                "lakebase_database_path": "/lakebase/foo",
                "sp_client_id": "sp-uuid",
            },
        )
        outputs = load_state_outputs(inst, "dev", ws=ws)
        assert outputs.variables.get("lakebase_project_name") == "demo-proj"
        assert outputs.variables.get("lakebase_branch_path") == "main"
        assert outputs.variables.get("lakebase_database_path") == "/lakebase/foo"
        # sp_client_id is in the schema but intentionally not in
        # state_keys, so it stays out of the variables dict.
        assert "sp_client_id" not in outputs.variables

    def test_empty_database_path_is_valid(self, tmp_path: Path):
        # ``provision_lakebase_project`` may persist an empty
        # ``lakebase_database_path`` when discovery fails -- still a
        # valid (required) string.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "lakebase",
            {
                "lakebase_project_name": "demo-proj",
                "lakebase_branch_path": "main",
                "lakebase_database_path": "",
                "sp_client_id": "sp-uuid",
            },
        )
        outputs = load_state_outputs(inst, "dev", ws=ws)
        assert outputs.variables["lakebase_database_path"] == ""

    def test_missing_sp_client_id_is_tolerated(self, tmp_path: Path):
        # ``sp_client_id`` is declared optional so state files written
        # before the SP-rotation feature (which only had the three
        # core paths) still load cleanly.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "lakebase",
            {
                "lakebase_project_name": "demo-proj",
                "lakebase_branch_path": "main",
                "lakebase_database_path": "/x",
            },
        )
        outputs = load_state_outputs(inst, "dev", ws=ws)
        assert outputs.variables["lakebase_project_name"] == "demo-proj"

    def test_missing_required_field_raises(self, tmp_path: Path):
        # The three core paths ARE required; missing one fails fast
        # via the agent_app brick's load_state_outputs hook.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        _seed_state(
            ws,
            inst,
            "lakebase",
            {
                "lakebase_project_name": "demo-proj",
                # lakebase_branch_path missing
                "lakebase_database_path": "/x",
                "sp_client_id": "sp-uuid",
            },
        )
        with pytest.raises(BrickStateValidationError) as exc:
            load_state_outputs(inst, "dev", ws=ws)
        # Owned by the ``app`` brick now; the missing field is still
        # surfaced explicitly so authors can fix the state file.
        assert "app" in str(exc.value)
        assert "lakebase_branch_path" in str(exc.value)


class TestMcpServerSchema:
    """#176: MCP brick state_schema.

    State is seeded into an in-memory workspace fake.

    Since issue #338 MCP is no longer a registered orchestrator brick
    (it is a capability of the agent payload, driven by
    :class:`payloads.agent.deploy.mcp.McpStep`), so these tests exercise
    ``MCPBrick.load_state_outputs`` directly -- the exact code path the
    step delegates to -- rather than the orchestrator aggregate.
    """

    def _outputs(self, ws: FakeWorkspaceClient, inst: Path) -> dict[str, str]:
        """Run ``MCPBrick.load_state_outputs`` against the workspace state."""
        from payloads.agent.deploy.mcp.brick import MCPBrick

        return MCPBrick().load_state_outputs(DeployContext(instance_dir=inst, target="dev", ws=ws))

    def _valid_state(self) -> dict:
        # Minimum viable state that ``deploy.py:_build_state`` would
        # ever produce.  ``allow`` carries one bare string and one
        # dict to exercise the union shape that real managed-Genie
        # / managed-Functions servers produce via ``expose_as``.
        return {
            "url": "https://host/api/2.0/mcp/genie/space-1",
            "type": "managed_genie",
            "name_prefix": "genie__",
            "tools": {
                "allow": [
                    "query_space_*",
                    {"match": "list_spaces", "expose_as": "list"},
                ],
                "name_prefix": "genie__",
                "default_meta": {},
            },
            "prompts": {"enabled": False, "allow": []},
            "resources": {"enabled": False, "allow": []},
        }

    def _write(self, ws: FakeWorkspaceClient, inst: Path, sub: str, payload: dict) -> None:
        from agent_foundry._instance_layout import resolve_payload_subdir

        # MCP authoring lives under the agent payload root since #338.
        authoring_dir = resolve_payload_subdir(inst, "mcp_servers") / sub
        _seed_state(ws, inst, "mcp_servers", payload, sub=sub, authoring_dir=authoring_dir)

    def test_schema_declared(self):
        from payloads.agent.deploy.mcp.brick import MCPBrick, McpServerState

        assert MCPBrick.state_schema is McpServerState

    def test_valid_state_loads(self, tmp_path: Path):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        self._write(ws, inst, "genie", self._valid_state())
        outputs = self._outputs(ws, inst)
        assert outputs["mcp__genie"] == "https://host/api/2.0/mcp/genie/space-1"

    def test_audit_trail_tolerated(self, tmp_path: Path):
        # Workspace-backed save_state appends an _audit list to every
        # persisted state file.  Validation must not flag it.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        payload = self._valid_state()
        payload["_audit"] = [
            {
                "action": "deploy",
                "deployer": "ci",
                "timestamp": "2026-01-01T00:00:00Z",
            }
        ]
        self._write(ws, inst, "genie", payload)
        outputs = self._outputs(ws, inst)
        assert "mcp__genie" in outputs

    def test_auto_poll_absent_is_valid(self, tmp_path: Path):
        # _build_state intentionally omits auto_poll when the resolved
        # config is None (deploy.py:126-127); absence != off-vs-null.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        payload = self._valid_state()
        assert "auto_poll" not in payload
        self._write(ws, inst, "genie", payload)
        outputs = self._outputs(ws, inst)
        assert "mcp__genie" in outputs

    def test_auto_poll_present_as_dict_is_valid(self, tmp_path: Path):
        # When set, auto_poll is the model_dump of AutoPollConfig --
        # any well-formed dict.  We accept it without re-validating
        # the inner shape (deploy time owns that contract).
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        payload = self._valid_state()
        payload["auto_poll"] = {
            "trigger": "query_space",
            "poll_with": "poll_response",
            "forward_args": [
                {"from_field": "conversationId", "to_arg": "conversation_id"},
            ],
            "state_field": "status",
            "pending_states": ["PENDING"],
            "timeout_s": 300.0,
        }
        self._write(ws, inst, "genie", payload)
        outputs = self._outputs(ws, inst)
        assert "mcp__genie" in outputs

    @pytest.mark.parametrize(
        "missing_key",
        ["url", "type", "name_prefix", "tools", "prompts", "resources"],
    )
    def test_missing_required_top_level_field_raises(
        self,
        tmp_path: Path,
        missing_key: str,
    ):
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        payload = self._valid_state()
        payload.pop(missing_key)
        self._write(ws, inst, "genie", payload)

        with pytest.raises(BrickStateValidationError) as exc:
            self._outputs(ws, inst)
        msg = str(exc.value)
        assert "mcp" in msg
        assert "genie" in msg  # sub-instance pinpointed
        assert missing_key in msg

    def test_nested_submodel_violation_raises(self, tmp_path: Path):
        # ``tools`` must be an object, not a list.  Confirms nested
        # validation propagates through to BrickStateValidationError.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        payload = self._valid_state()
        payload["tools"] = ["not", "an", "object"]
        self._write(ws, inst, "genie", payload)

        with pytest.raises(BrickStateValidationError) as exc:
            self._outputs(ws, inst)
        assert "mcp" in str(exc.value)

    def test_two_servers_no_collision(self, tmp_path: Path):
        # Nested-layout regression guard mirroring the genie test.
        inst = tmp_path / "app"
        ws = FakeWorkspaceClient()
        for sub, url in [
            ("genie", "https://host/api/2.0/mcp/genie/s1"),
            ("functions", "https://host/api/2.0/mcp/functions/c/s"),
        ]:
            payload = self._valid_state()
            payload["url"] = url
            self._write(ws, inst, sub, payload)

        outputs = self._outputs(ws, inst)
        assert outputs["mcp__genie"] == "https://host/api/2.0/mcp/genie/s1"
        assert outputs["mcp__functions"] == "https://host/api/2.0/mcp/functions/c/s"


class TestObservabilityDashboardSchema:
    """#177: Observability brick state_schema.

    The observability brick is the only follow-up that does NOT
    subclass ``StandardLayoutBrick``: its state lives only in the
    workspace under ``brick_type="dashboard"`` (see issue #50, no
    local cache).  Validation must therefore happen inside the
    brick's own ``load_state_outputs`` rather than via the
    standard-layout hook -- these tests cover both the schema
    declaration and the in-method validation flow.

    No outputs are published: the audit for #157 found no actual
    downstream consumer of ``dashboard_id`` / ``etag`` /
    ``display_name``.  See ``ObservabilityBrick.load_state_outputs``
    for the rationale.
    """

    def _make_ctx(self, ws_state: dict | None, tmp_path: Path):
        """Build a DeployContext with a fake workspace returning ``ws_state``.

        ``ws_state=None`` means "no state in workspace" (the load
        helper returns ``{}`` and validation is skipped).
        """
        from unittest.mock import MagicMock

        from agent_foundry.bricks import DeployContext

        ws = MagicMock()
        if ws_state is None:
            # workspace.export raises -- _read_workspace_file returns None.
            ws.workspace.export.side_effect = Exception("not found")
        else:
            # _read_workspace_file consumes ``resp.content`` and JSON-
            # decodes bytes as utf-8.
            resp = MagicMock()
            resp.content = json.dumps(ws_state).encode("utf-8")
            ws.workspace.export.return_value = resp
        inst = tmp_path / "app"
        inst.mkdir()
        return DeployContext(instance_dir=inst, target="dev", ws=ws)

    def test_schema_declared(self):
        from bricks.observability.brick import DashboardState, ObservabilityBrick

        assert ObservabilityBrick.state_schema is DashboardState

    def test_no_outputs_published(self, tmp_path: Path):
        # Audit finding (#157): no downstream brick consumes these.
        # The brick's contract is to validate, not publish.
        from bricks.observability.brick import ObservabilityBrick

        ctx = self._make_ctx(
            {
                "dashboard_id": "dash-abc",
                "etag": "v1",
                "display_name": "[dev] app-dashboard",
            },
            tmp_path,
        )
        outputs = ObservabilityBrick().load_state_outputs(ctx)
        assert outputs == {}

    def test_valid_state_validates_cleanly(self, tmp_path: Path):
        from bricks.observability.brick import ObservabilityBrick

        ctx = self._make_ctx(
            {
                "dashboard_id": "dash-abc",
                "etag": "v1",
                "display_name": "[dev] app-dashboard",
                "_audit": [{"action": "create"}],  # tolerated
            },
            tmp_path,
        )
        # Must not raise.
        ObservabilityBrick().load_state_outputs(ctx)

    def test_no_workspace_state_skips_validation(self, tmp_path: Path):
        # First-time deploy: the workspace has nothing yet.  Must not
        # raise -- "empty == not yet deployed".
        from bricks.observability.brick import ObservabilityBrick

        ctx = self._make_ctx(None, tmp_path)
        outputs = ObservabilityBrick().load_state_outputs(ctx)
        assert outputs == {}

    @pytest.mark.parametrize(
        "missing_key",
        ["dashboard_id", "etag", "display_name"],
    )
    def test_missing_required_field_raises(self, tmp_path: Path, missing_key: str):
        from bricks.observability.brick import ObservabilityBrick

        full = {
            "dashboard_id": "dash-abc",
            "etag": "v1",
            "display_name": "[dev] app-dashboard",
        }
        full.pop(missing_key)
        ctx = self._make_ctx(full, tmp_path)

        with pytest.raises(BrickStateValidationError) as exc:
            ObservabilityBrick().load_state_outputs(ctx)
        msg = str(exc.value)
        assert "observability" in msg
        assert missing_key in msg

    def test_validation_propagates_through_aggregator(self, tmp_path: Path):
        # End-to-end check that the brick's in-method validation is
        # picked up by ``agent_foundry.config.load_state_outputs`` --
        # a malformed dashboard state file must halt the public
        # aggregator the same way #156 halts standard-layout bricks.
        from agent_foundry import load_state_outputs as agg_load

        ctx = self._make_ctx(
            {"dashboard_id": "dash-abc"},  # missing etag + display_name
            tmp_path,
        )
        with pytest.raises(BrickStateValidationError):
            agg_load(ctx.instance_dir, "dev", ws=ctx.ws)
