"""Tests for the app/ instance layout and the payload-adapter seam (#338).

Covers:
* ``resolve_app_config_dir`` -- always ``app/``; a leftover
  ``agent_app/`` is an error.
* ``_load_payload_config`` -- defaults to the ``agent`` adapter, accepts
  an explicit one, and rejects an unknown adapter / malformed file.
* the framework adapter registry (``get_payload_adapter``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
import yaml

pytest.importorskip("databricks.sdk", reason="databricks-sdk not installed")

from agent_foundry._instance_layout import discover_payloads, payload_root  # noqa: E402
from agent_foundry.bricks import (  # noqa: E402
    BrickResult,
    DeployContext,
    DestroyContext,
    DestroyPlanItem,
)
from agent_foundry.deploy import payload_adapters as _pa  # noqa: E402
from agent_foundry.deploy.app_host import AppInfraSpec, AppPayloadSpec  # noqa: E402
from agent_foundry.deploy.payload_adapters import (  # noqa: E402
    AppPayloadAdapter,
    get_payload_adapter,
    iter_payload_adapters,
)
from agent_foundry.deploy.payload_adapters import (
    load_payload_config as _load_payload_config,
)
from agent_foundry.loader import FoundryConfigError  # noqa: E402
from agent_foundry.scaffold import build_scaffold_registry  # noqa: E402
from agent_foundry.scaffold_spec import ScaffoldDecl, ScaffoldSource  # noqa: E402
from payloads.agent.adapter import AgentPayloadAdapter  # noqa: E402
from payloads.agent.config import resolve_agent_spec  # noqa: E402
from payloads.agent.deploy.config import (  # noqa: E402
    agent_runtime_variables,
    resolve_app_config_dir,
    wheel_extras_from_spec,
)
from payloads.agent.deploy.spec import _resolve_app_payload  # noqa: E402
from payloads.rest_mcp.adapter import RestMcpPayloadAdapter  # noqa: E402

_APP_YAML = {
    "command": ["python", "-m", "app"],
    "env": [{"name": "INSTANCE_DIR", "value": "instances/${var.app_name}"}],
}


# ---------------------------------------------------------------------------
# resolve_app_config_dir
# ---------------------------------------------------------------------------


class TestResolveAppConfigDir:
    def test_returns_app_dir(self, tmp_path: Path) -> None:
        (tmp_path / "app").mkdir()
        assert resolve_app_config_dir(tmp_path) == tmp_path / "app"

    def test_defaults_to_app_when_absent(self, tmp_path: Path) -> None:
        assert resolve_app_config_dir(tmp_path) == tmp_path / "app"

    def test_leftover_agent_app_dir_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "app").mkdir()
        (tmp_path / "agent_app").mkdir()
        with pytest.raises(FoundryConfigError, match="rename it to 'app/'"):
            resolve_app_config_dir(tmp_path)


# ---------------------------------------------------------------------------
# _load_payload_config
# ---------------------------------------------------------------------------


class TestLoadPayloadConfig:
    def test_defaults_to_agent_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "app").mkdir()
        cfg = _load_payload_config(tmp_path)
        assert cfg.adapter == "agent"
        assert cfg.requires == ["tracing", "observability"]
        assert cfg.spec == {}

    def test_defaults_to_agent_when_no_instance(self) -> None:
        assert _load_payload_config(None).adapter == "agent"
        assert _load_payload_config(None).requires == ["tracing", "observability"]

    def test_reads_explicit_adapter_and_spec(self, tmp_path: Path) -> None:
        cfg_dir = payload_root(tmp_path)
        cfg_dir.mkdir(parents=True)
        spec = {"runtime": {"tools": {"mcp_only": True}}}
        (cfg_dir / "payload.yml").write_text(yaml.dump({"payload": {"adapter": "agent", "spec": spec}}))
        cfg = _load_payload_config(tmp_path)
        assert cfg.adapter == "agent"
        assert cfg.spec == spec

    def test_rejects_unknown_top_level_key(self, tmp_path: Path) -> None:
        cfg_dir = payload_root(tmp_path)
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "payload.yml").write_text(yaml.dump({"payload": {"adapter": "agent"}, "typo": 1}))
        with pytest.raises(FoundryConfigError, match="payload.yml"):
            _load_payload_config(tmp_path)

    def test_discovers_non_agent_payload(self, tmp_path: Path) -> None:
        # A ``rest_mcp`` instance carries its manifest at
        # ``payloads/rest_mcp/payload.yml`` -- discovery must select it
        # without the framework hard-coding the ``agent`` payload dir (#326).
        cfg_dir = payload_root(tmp_path, "rest_mcp")
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "payload.yml").write_text(yaml.dump({"payload": {"adapter": "rest_mcp", "spec": {"x": 1}}}))
        cfg = _load_payload_config(tmp_path)
        assert cfg.adapter == "rest_mcp"
        assert cfg.spec == {"x": 1}

    def test_raises_on_multiple_payload_manifests(self, tmp_path: Path) -> None:
        for name in ("agent", "rest_mcp"):
            cfg_dir = payload_root(tmp_path, name)
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "payload.yml").write_text(yaml.dump({"payload": {"adapter": name}}))
        with pytest.raises(FoundryConfigError, match="multiple payload manifests"):
            _load_payload_config(tmp_path)


class TestDiscoverPayloads:
    def test_empty_when_no_payloads_dir(self, tmp_path: Path) -> None:
        assert discover_payloads(tmp_path) == []

    def test_lists_only_dirs_with_a_manifest_sorted(self, tmp_path: Path) -> None:
        for name in ("rest_mcp", "agent"):
            cfg_dir = payload_root(tmp_path, name)
            cfg_dir.mkdir(parents=True)
            (cfg_dir / "payload.yml").write_text(yaml.dump({"payload": {"adapter": name}}))
        # A payload dir without a manifest is not discovered.
        payload_root(tmp_path, "no_manifest").mkdir(parents=True)
        assert discover_payloads(tmp_path) == ["agent", "rest_mcp"]


# ---------------------------------------------------------------------------
# payload adapter registry
# ---------------------------------------------------------------------------


class TestPayloadAdapterRegistry:
    def test_agent_adapter_registered(self) -> None:
        adapter = get_payload_adapter("agent")
        assert isinstance(adapter, AgentPayloadAdapter)
        assert adapter.name == "agent"

    def test_rest_mcp_adapter_registered(self) -> None:
        adapter = get_payload_adapter("rest_mcp")
        assert isinstance(adapter, RestMcpPayloadAdapter)
        assert adapter.name == "rest_mcp"

    def test_unknown_adapter_raises(self) -> None:
        with pytest.raises(FoundryConfigError, match="Unknown payload adapter"):
            get_payload_adapter("does-not-exist")

    def test_builtin_adapters_satisfy_protocol(self) -> None:
        for adapter in iter_payload_adapters():
            assert isinstance(adapter, AppPayloadAdapter)

    def test_agent_adapter_applies_to_agent_instance(self, tmp_path: Path) -> None:
        adapter = AgentPayloadAdapter()
        assert adapter.applies_to(tmp_path, {}) is False
        # The agent payload claims an instance by the presence of its
        # config surface at ``payloads/agent/`` (issue #338).
        payload_root(tmp_path).mkdir(parents=True)
        assert adapter.applies_to(tmp_path, {}) is True

    def test_agent_adapter_build_matches_resolve(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "app"
        cfg_dir.mkdir()
        (cfg_dir / "app.yaml").write_text(yaml.dump(_APP_YAML))

        via_adapter = AgentPayloadAdapter().build_payload_spec(tmp_path, {"app_name": "x"}, {})

        # Mirror the adapter's seam: resolve the (defaulted) spec, publish
        # the agent-derived variables, then resolve the runtime payload.
        spec = resolve_agent_spec(tmp_path, {})
        variables = {"app_name": "x"}
        variables = {**variables, **agent_runtime_variables(spec, variables)}
        direct = _resolve_app_payload(
            tmp_path,
            variables,
            requirements_extras=wheel_extras_from_spec(spec),
            spec=spec,
        )
        assert isinstance(via_adapter, AppPayloadSpec)
        assert via_adapter == direct


# ---------------------------------------------------------------------------
# payload deploy-contract conformance (#338)
# ---------------------------------------------------------------------------


class TestPayloadDeployContract:
    """The agent payload satisfies the full deploy-time contract: the
    framework App host depends only on these structural hooks."""

    def test_implements_all_hooks(self) -> None:
        adapter = AgentPayloadAdapter()
        for hook in ("build_payload_spec", "build_infra", "after_reconcile", "provision", "plan_destroy", "destroy"):
            assert callable(getattr(adapter, hook)), f"missing hook: {hook}"

    def test_provision_and_destroy_no_op_when_chat_history_disabled(self, tmp_path: Path) -> None:
        """With chat history off, the deploy hooks are inert offline --
        no Lakebase, no SDK, no teardown items."""
        from agent_foundry.bricks import DeployContext, DestroyContext

        adapter = AgentPayloadAdapter()
        inst = tmp_path / "inst"
        inst.mkdir()
        payload_dir = inst / "payloads" / "agent"
        payload_dir.mkdir(parents=True)
        (payload_dir / "payload.yml").write_text(
            "payload:\n  adapter: agent\n  spec:\n    ui:\n      chat_history:\n        enabled: false\n",
            encoding="utf-8",
        )

        deploy_ctx = DeployContext(
            instance_dir=inst,
            target="dev",
            variables={"chat_history_enabled": "false"},
            dry_run=True,
            config=None,
        )
        assert adapter.provision(deploy_ctx) == 0

        destroy_ctx = DestroyContext(
            instance_dir=inst,
            target="dev",
            variables={"chat_history_enabled": "false"},
            dry_run=True,
            config=None,
            force=True,
        )
        assert adapter.plan_destroy(destroy_ctx) == []
        assert adapter.destroy(destroy_ctx) == []


# ---------------------------------------------------------------------------
# Payload scaffold declarations (#368)
# ---------------------------------------------------------------------------


class TestAgentAdapterScaffolds:
    """The agent payload contributes its authoring surfaces as scaffold
    declarations rather than the framework hard-coding them (#368)."""

    def test_declares_expected_verbs(self) -> None:
        names = {decl.name for decl in AgentPayloadAdapter().scaffolds()}
        assert names == {"payload", "skills", "mcp", "speech_input"}

    def test_all_declared_verbs_share_the_payload_group(self) -> None:
        for decl in AgentPayloadAdapter().scaffolds():
            assert decl.group == "agent payload"

    def test_scaffold_sources_exist_on_disk(self) -> None:
        """Guards against a packaging bug: every declared source must
        ship with the payload package."""
        for decl in AgentPayloadAdapter().scaffolds():
            for src in decl.sources:
                assert src.source.exists(), f"{decl.name!r}: missing scaffold source {src.source}"

    def test_destinations_land_under_the_payload_subdir(self) -> None:
        """Except the speech model: a Model Serving sub-instance the brick deploys."""
        for decl in AgentPayloadAdapter().scaffolds():
            for src in decl.sources:
                assert src.destination.startswith("payloads/agent/") or src.destination == "model_serving/speech"


# ---------------------------------------------------------------------------
# Third-party adapter discovery via entry points (#368)
# ---------------------------------------------------------------------------


class _FakePayloadAdapter:
    """Minimal structural :class:`AppPayloadAdapter` for discovery tests."""

    name = "fake_payload"

    def applies_to(self, instance_dir: Path, config: object) -> bool:  # noqa: ARG002
        return False

    def build_payload_spec(
        self,
        instance_dir: Path | None,
        variables: Mapping[str, str],
        spec: Mapping[str, object],
    ) -> AppPayloadSpec:
        raise NotImplementedError

    def build_infra(
        self,
        instance_dir: Path | None,
        variables: Mapping[str, str],
        *,
        target: str,
    ) -> AppInfraSpec:
        raise NotImplementedError

    def after_reconcile(
        self,
        ws: object,
        app_name: str,
        variables: Mapping[str, str],
        *,
        dry_run: bool,
    ) -> None:
        return None

    def provision(self, ctx: DeployContext) -> int:
        return 0

    def plan_destroy(self, ctx: DestroyContext) -> list[DestroyPlanItem]:
        return []

    def destroy(self, ctx: DestroyContext) -> list[BrickResult]:
        return []

    def load_state_outputs(self, ctx: DeployContext) -> dict[str, str]:
        return {}

    def scaffolds(self) -> Sequence[ScaffoldDecl]:
        return (
            ScaffoldDecl(
                name="fake_payload",
                group="fake payload",
                display="Fake Payload",
                summary="A fake payload scaffold for tests.",
                sources=(ScaffoldSource(Path("/does/not/exist.yml"), "payloads/fake/payload.yml", "file"),),
            ),
        )


class _FakeEntryPoint:
    def __init__(self, obj: object, *, name: str = "fake_payload") -> None:
        self._obj = obj
        self.name = name
        self.value = "tests:_FakePayloadAdapter"

    def load(self) -> object:
        return self._obj


def _patch_entry_points(monkeypatch: pytest.MonkeyPatch, eps: list[object]) -> None:
    """Point the adapter loader at *eps* for our group, nothing otherwise."""

    def fake_entry_points(*, group: str) -> list[object]:
        return eps if group == _pa.ENTRY_POINT_GROUP else []

    monkeypatch.setattr(
        "agent_foundry.deploy.payload_adapters._metadata.entry_points",
        fake_entry_points,
    )


class TestThirdPartyAdapterDiscovery:
    def test_entry_point_adapter_is_discovered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_entry_points(monkeypatch, [_FakeEntryPoint(_FakePayloadAdapter)])
        names = [a.name for a in iter_payload_adapters()]
        assert "fake_payload" in names
        # Built-ins remain registered alongside third-party adapters.
        assert "agent" in names

    def test_class_or_instance_both_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_entry_points(monkeypatch, [_FakeEntryPoint(_FakePayloadAdapter())])
        assert "fake_payload" in [a.name for a in iter_payload_adapters()]

    def test_third_party_overrides_builtin_by_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _OverrideAgent(_FakePayloadAdapter):
            name = "agent"

        _patch_entry_points(monkeypatch, [_FakeEntryPoint(_OverrideAgent, name="agent")])
        by_name = {a.name: a for a in iter_payload_adapters()}
        assert isinstance(by_name["agent"], _OverrideAgent)

    def test_load_failure_raises_runtimeerror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _BoomEP:
            name = "boom"
            value = "x:Y"

            def load(self) -> object:
                raise ImportError("nope")

        _patch_entry_points(monkeypatch, [_BoomEP()])
        with pytest.raises(RuntimeError, match="Failed to load"):
            iter_payload_adapters()

    def test_non_conforming_adapter_raises_runtimeerror(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _NotAnAdapter:
            name = "nope"  # missing the rest of the protocol

        _patch_entry_points(monkeypatch, [_FakeEntryPoint(_NotAnAdapter, name="nope")])
        with pytest.raises(RuntimeError, match="does not implement"):
            iter_payload_adapters()


class TestScaffoldRegistryComposition:
    def test_registry_merges_framework_and_payload_scaffolds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_entry_points(monkeypatch, [_FakeEntryPoint(_FakePayloadAdapter)])
        registry = build_scaffold_registry()
        assert "warehouse" in registry  # framework brick
        assert "payload" in registry  # built-in agent payload
        assert "fake_payload" in registry  # third-party payload

    def test_duplicate_scaffold_name_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _CollidingAdapter(_FakePayloadAdapter):
            name = "collider"

            def scaffolds(self) -> Sequence[ScaffoldDecl]:
                return (
                    ScaffoldDecl(
                        name="warehouse",  # collides with a framework brick
                        group="collider",
                        display="Collision",
                        summary="Shadows the framework warehouse scaffold.",
                        sources=(),
                    ),
                )

        _patch_entry_points(monkeypatch, [_FakeEntryPoint(_CollidingAdapter, name="collider")])
        with pytest.raises(ValueError, match="Duplicate scaffold name"):
            build_scaffold_registry()
