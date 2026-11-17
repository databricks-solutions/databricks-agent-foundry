"""Scenario catalog, identity, wizard answers, and workspace binding."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from urllib.error import HTTPError

import pytest
import yaml

from agent_foundry.init.examples_registry import VariantManifest
from agent_foundry.testing.fixtures import PAYLOAD_PATCH_FILENAME, FixtureError, apply_fixture, fixture_dir
from agent_foundry.testing.harness import wizard_stdin
from agent_foundry.testing.identity import IdentityError, make_run_identity
from agent_foundry.testing.janitor import matching_app_names
from agent_foundry.testing.lifecycle import run_then_destroy
from agent_foundry.testing.readiness import ReadinessError, authenticated_post_json, wait_for_app
from agent_foundry.testing.scenarios import Scenario, ScenarioCatalogError, load_scenarios
from agent_foundry.testing.workspace import (
    WorkspaceConfigError,
    assert_host_matches_sdk,
    load_workspace_binding,
)


def test_variant_manifest_stays_display_only() -> None:
    VariantManifest.model_validate(
        {"display_name": "X", "description": "Y"},
    )
    with pytest.raises(Exception):
        VariantManifest.model_validate(
            {
                "display_name": "X",
                "description": "Y",
                "lane": "canary",
            }
        )


def test_bundled_catalog_known_variants() -> None:
    by_id = {s.id: s for s in load_scenarios()}
    assert list(by_id) == [
        "bricks",
        "agent_payload",
        "rest_mcp_payload",
        "rest_mcp_proxy",
        "portfolio_assistant",
        "unity_gateway_assistant",
    ]
    assert by_id["rest_mcp_payload"].lane == "full"
    assert by_id["rest_mcp_proxy"].lane == "canary"
    assert by_id["portfolio_assistant"].lane == "full"
    assert "warehouse" in by_id["portfolio_assistant"].requires
    assert by_id["unity_gateway_assistant"].kind == "example"
    assert by_id["unity_gateway_assistant"].lane == "full"


def _scenario(**overrides: object) -> Scenario:
    fields: dict[str, object] = {
        "id": "bricks",
        "kind": "brick",
        "variant_id": "blank",
        "lane": "canary",
        "timeout_minutes": 10,
        "scaffolds": ("warehouse",),
        "checks": ("bricks/warehouse/tests/live",),
    }
    fields.update(overrides)
    return Scenario.model_validate(fields)


def test_bundled_brick_canary_pairs_scaffolds_with_checks() -> None:
    bricks = next(s for s in load_scenarios() if s.kind == "brick")
    assert bricks.variant_id == "blank"
    assert bricks.scaffolds == (
        "warehouse",
        "data_domains",
        "sql_function",
        "genie_spaces",
        "vector_indexes",
        "jobs",
    )


def test_bundled_checks_exist() -> None:
    repo = Path(__file__).resolve().parents[3]
    missing = [check for s in load_scenarios() for check in s.checks if not (repo / check).exists()]
    assert not missing


def test_blank_brick_scenario_is_valid() -> None:
    scenario = _scenario()
    assert scenario.scaffolds == ("warehouse",)
    assert scenario.checks == ("bricks/warehouse/tests/live",)


def test_payload_scenario_needs_no_checks() -> None:
    """Payload canaries gate on ``agent-foundry smoke``, not tests/live paths."""
    scenario = _scenario(kind="payload", checks=())
    assert scenario.checks == ()


def test_blank_variant_passes_known_variant_check(tmp_path: Path) -> None:
    catalog = tmp_path / "scenarios.toml"
    catalog.write_text(
        '[[scenarios]]\nid = "bricks"\nkind = "brick"\nvariant_id = "blank"\nlane = "canary"\n'
        'timeout_minutes = 10\nscaffolds = ["warehouse"]\nchecks = ["bricks/warehouse/tests/live"]\n',
        encoding="utf-8",
    )
    assert [s.id for s in load_scenarios(catalog)] == ["bricks"]


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"kind": "example"}, "not 'blank'"),
        ({"scaffolds": ()}, "needs scaffolds"),
        ({"checks": ()}, "at least one tests/live check"),
        ({"checks": ("bricks/a/tests/live", "bricks/b/tests/live")}, "lengths differ"),
        ({"scaffolds": ("warehouse; rm -rf /",)}, "lowercase identifiers"),
        ({"checks": ("bricks/warehouse/tests/unit",)}, "under a tests/live folder"),
        ({"checks": ("../outside/tests/live",)}, "under a tests/live folder"),
        ({"checks": ("/abs/tests/live",)}, "under a tests/live folder"),
        ({"checks": ("bricks/tests/live $(id)",)}, "under a tests/live folder"),
    ],
)
def test_scenario_shape_is_enforced(overrides: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        _scenario(**overrides)


def test_harness_matrix_filters_by_kind(capsys: pytest.CaptureFixture[str]) -> None:
    import json

    from agent_foundry.testing.harness import main

    assert main(["matrix", "--kind", "brick"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["bricks"]
    assert main(["matrix", "--kind", "payload"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == ["agent_payload", "rest_mcp_payload"]
    assert main(["matrix", "--kind", "example"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["kind"] for r in rows] == ["example"] * len(rows) and rows


def test_every_fixture_scenario_has_a_fixture_tree() -> None:
    with_fixture = [s.id for s in load_scenarios() if s.fixture]
    assert with_fixture == ["agent_payload"]
    for scenario_id in with_fixture:
        assert any(path.is_file() for path in fixture_dir(scenario_id).rglob("*"))


def test_apply_fixture_adds_files_and_never_replaces(tmp_path: Path) -> None:
    source = tmp_path / "fixture"
    (source / "payloads" / "agent").mkdir(parents=True)
    (source / "payloads" / "agent" / "a.yml").write_text("a: 1\n")
    (source / "smoke.yml").write_text("questions: []\n")
    instance = tmp_path / "instance"
    instance.mkdir()

    added = apply_fixture(source, instance)
    assert added == [Path("payloads/agent/a.yml"), Path("smoke.yml")]
    assert (instance / "payloads" / "agent" / "a.yml").read_text() == "a: 1\n"

    (instance / "payloads" / "agent" / "a.yml").unlink()
    with pytest.raises(FixtureError, match="smoke.yml"):
        apply_fixture(source, instance)
    assert not (instance / "payloads" / "agent" / "a.yml").exists()


def _write_payload(instance: Path, text: str) -> Path:
    payload = instance / "payloads" / "agent" / "payload.yml"
    payload.parent.mkdir(parents=True, exist_ok=True)
    payload.write_text(text)
    return payload


def test_apply_fixture_merges_the_payload_patch_instead_of_copying_it(tmp_path: Path) -> None:
    source = tmp_path / "fixture"
    source.mkdir()
    (source / PAYLOAD_PATCH_FILENAME).write_text("payload:\n  spec:\n    a: 2\n    new: {b: 3}\n")
    instance = tmp_path / "instance"
    payload = _write_payload(instance, "payload:\n  adapter: agent\n  spec:\n    a: 1\n    keep: x\n")

    assert apply_fixture(source, instance) == [Path("payloads/agent/payload.yml")]
    assert yaml.safe_load(payload.read_text()) == {
        "payload": {"adapter": "agent", "spec": {"a": 2, "keep": "x", "new": {"b": 3}}}
    }
    assert not (instance / PAYLOAD_PATCH_FILENAME).exists()


def test_payload_patch_needs_a_scaffolded_payload(tmp_path: Path) -> None:
    source = tmp_path / "fixture"
    source.mkdir()
    (source / PAYLOAD_PATCH_FILENAME).write_text("payload: {}\n")
    (source / "smoke.yml").write_text("questions: []\n")
    instance = tmp_path / "instance"
    instance.mkdir()

    with pytest.raises(FixtureError, match="needs a scaffolded"):
        apply_fixture(source, instance)
    assert not (instance / "smoke.yml").exists()


def test_harness_apply_fixture_skips_scenarios_without_one(tmp_path: Path) -> None:
    from agent_foundry.testing.harness import main

    assert main(["apply-fixture", "--scenario", "rest_mcp_payload", "--instance-dir", str(tmp_path)]) == 0
    assert not any(tmp_path.iterdir())
    payload = _write_payload(tmp_path, "payload:\n  adapter: agent\n")
    assert main(["apply-fixture", "--scenario", "agent_payload", "--instance-dir", str(tmp_path)]) == 0
    assert (tmp_path / "smoke.yml").is_file()
    assert (tmp_path / "payloads" / "agent" / "mcp_servers" / "sql_warehouse" / "server.yml").is_file()
    assert "compaction" in yaml.safe_load(payload.read_text())["payload"]["spec"]["runtime"]["context"]


def test_unknown_variant_id_fails(tmp_path: Path) -> None:
    catalog = tmp_path / "scenarios.toml"
    catalog.write_text(
        '[[scenarios]]\nid = "x"\nkind = "example"\nvariant_id = "not_a_real_variant"\n'
        'lane = "canary"\ntimeout_minutes = 10\n',
        encoding="utf-8",
    )
    with pytest.raises(ScenarioCatalogError, match="unknown variant_id"):
        load_scenarios(catalog)


def test_env_filter_unknown_id_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS", "nope")
    with pytest.raises(ScenarioCatalogError, match="unknown scenario"):
        load_scenarios()


def _by_id(scenario_id: str) -> Scenario:
    return next(s for s in load_scenarios() if s.id == scenario_id)


def test_env_filter_selects_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FOUNDRY_ACCEPTANCE_SCENARIOS", "rest_mcp_proxy")
    scenarios = load_scenarios()
    assert [s.id for s in scenarios] == ["rest_mcp_proxy"]


def test_run_identity_sanitizes_and_fits_app_limit() -> None:
    scenario = _by_id("rest_mcp_proxy")
    ident = make_run_identity(scenario, "GH-123456789")
    assert ident.project_name.startswith("af-rm-")
    assert ident.project_name == ident.project_name.lower()
    assert "-" not in ident.schema_prefix
    assert len(ident.app_name) <= 26
    assert len(f"dev-{ident.app_name}-app") <= 63


def test_default_run_identity_is_stable_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_FOUNDRY_RUN_ID", raising=False)
    scenario = _by_id("rest_mcp_proxy")
    ident = make_run_identity(scenario)
    assert ident.run_token == "canary"
    assert ident.project_name == "af-rm-canary"


def test_empty_run_id_fails() -> None:
    scenario = _by_id("rest_mcp_proxy")
    with pytest.raises(IdentityError):
        make_run_identity(scenario, "---")


def test_workspace_binding_requires_host_and_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABRICKS_HOST", raising=False)
    monkeypatch.delenv("AGENT_FOUNDRY_TEST_CATALOG", raising=False)
    with pytest.raises(WorkspaceConfigError, match="DATABRICKS_HOST"):
        load_workspace_binding()


def test_workspace_binding_accepts_pat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("AGENT_FOUNDRY_TEST_CATALOG", "ci_cat")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi123")
    monkeypatch.delenv("DATABRICKS_CLIENT_ID", raising=False)
    monkeypatch.delenv("DATABRICKS_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    binding = load_workspace_binding()
    assert binding.host.endswith("databricks.com")
    assert os.environ.get("DATABRICKS_TOKEN") == "dapi123"


def test_workspace_binding_accepts_oauth_m2m(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("AGENT_FOUNDRY_TEST_CATALOG", "ci_cat")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "cid")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "csecret")
    monkeypatch.delenv("DATABRICKS_TOKEN", raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    load_workspace_binding()
    assert "DATABRICKS_TOKEN" not in os.environ


def test_workspace_binding_prefers_m2m_when_pat_also_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("AGENT_FOUNDRY_TEST_CATALOG", "ci_cat")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi123")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "cid")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "csecret")
    load_workspace_binding()
    assert "DATABRICKS_TOKEN" not in os.environ
    assert os.environ["DATABRICKS_CLIENT_ID"] == "cid"


def test_workspace_binding_ignores_empty_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("AGENT_FOUNDRY_TEST_CATALOG", "ci_cat")
    monkeypatch.setenv("DATABRICKS_TOKEN", "")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "cid")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "csecret")
    load_workspace_binding()
    assert "DATABRICKS_TOKEN" not in os.environ


def test_workspace_binding_accepts_github_oidc(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("AGENT_FOUNDRY_TEST_CATALOG", "ci_cat")
    monkeypatch.setenv("DATABRICKS_AUTH_TYPE", "github-oidc")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "sp-uuid")
    for name in ("DATABRICKS_TOKEN", "DATABRICKS_CLIENT_SECRET", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    assert load_workspace_binding().catalog == "ci_cat"


def test_github_oidc_needs_client_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("AGENT_FOUNDRY_TEST_CATALOG", "ci_cat")
    monkeypatch.setenv("DATABRICKS_AUTH_TYPE", "github-oidc")
    for name in ("DATABRICKS_TOKEN", "DATABRICKS_CLIENT_ID", "DATABRICKS_CLIENT_SECRET", "DATABRICKS_CONFIG_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(WorkspaceConfigError, match="github-oidc"):
        load_workspace_binding()


def test_workspace_binding_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("AGENT_FOUNDRY_TEST_CATALOG", "ci_cat")
    for name in (
        "DATABRICKS_TOKEN",
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(WorkspaceConfigError, match="CLIENT_ID"):
        load_workspace_binding()


def test_client_for_binding_clears_skip_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import MagicMock, patch

    from agent_foundry.telemetry import product_kwargs
    from agent_foundry.testing.workspace import WorkspaceBinding, client_for_binding
    from agent_foundry.workspace_client import SKIP_DEFAULT_WORKSPACE_CLIENT_ENV

    monkeypatch.setenv(SKIP_DEFAULT_WORKSPACE_CLIENT_ENV, "true")
    binding = WorkspaceBinding(host="https://a.cloud.databricks.com", catalog="c")
    mock_ws = MagicMock()
    mock_ws.config.host = "https://a.cloud.databricks.com"
    with patch("databricks.sdk.WorkspaceClient", return_value=mock_ws) as cls:
        client_for_binding(binding)
    cls.assert_called_once_with(host="https://a.cloud.databricks.com", **product_kwargs())
    assert SKIP_DEFAULT_WORKSPACE_CLIENT_ENV not in os.environ


def test_mcp_gateway_error_carries_response_body(monkeypatch: pytest.MonkeyPatch) -> None:
    httpx = pytest.importorskip("httpx")
    from agent_foundry.testing.mcp_session import McpGatewayError, require_gateway_ok

    body = '{"error_code":"PERMISSION_DENIED","message":"User is missing USE CONNECTION"}'
    sent: dict[str, object] = {}

    def _post(url: str, *, headers: dict[str, str], json: object, timeout: float) -> object:
        sent["headers"] = headers
        return httpx.Response(403, text=body)

    monkeypatch.setattr(httpx, "post", _post)
    with pytest.raises(McpGatewayError, match="403 from https://h/api/2.0/mcp/external/c: .*USE CONNECTION"):
        require_gateway_ok("https://h/api/2.0/mcp/external/c", {"Authorization": "Bearer t"})
    assert sent["headers"] == {"Authorization": "Bearer t", "Accept": "application/json, text/event-stream"}


def test_mcp_gateway_ok_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    httpx = pytest.importorskip("httpx")
    from agent_foundry.testing.mcp_session import require_gateway_ok

    monkeypatch.setattr(httpx, "post", lambda *_a, **_k: httpx.Response(200, text="event: message"))
    require_gateway_ok("https://h/api/2.0/mcp/external/c", {"Authorization": "Bearer t"})


@asynccontextmanager
async def _fake_session(server_url: str, headers: dict[str, str]) -> AsyncIterator[str]:
    yield f"session:{server_url}:{headers['Authorization']}"


async def _session_name(session: str) -> str:
    return session


@pytest.mark.parametrize(("is_v2", "opener"), [(True, "_v2_session"), (False, "_v1_session")])
def test_mcp_session_uses_the_installed_major(monkeypatch: pytest.MonkeyPatch, is_v2: bool, opener: str) -> None:
    from agent_foundry.testing import mcp_session

    monkeypatch.setattr(mcp_session, "_is_mcp_v2", lambda: is_v2)
    monkeypatch.setattr(mcp_session, opener, _fake_session)
    result = asyncio.run(mcp_session._with_session("https://h/mcp", {"Authorization": "Bearer t"}, _session_name))
    assert result == "session:https://h/mcp:Bearer t"


def test_workspace_api_url_always_has_https() -> None:
    from agent_foundry.testing.workspace import WorkspaceBinding, workspace_api_url

    binding = WorkspaceBinding(host="https://a.cloud.databricks.com", catalog="c")
    url = workspace_api_url(binding, "/api/2.0/mcp/external/af-rm-canary_github_mcp")
    assert url == "https://a.cloud.databricks.com/api/2.0/mcp/external/af-rm-canary_github_mcp"


def test_host_mismatch_rejected() -> None:
    from agent_foundry.testing.workspace import WorkspaceBinding

    binding = WorkspaceBinding(
        host="https://a.cloud.databricks.com",
        catalog="c",
    )
    ws = SimpleNamespace(config=SimpleNamespace(host="https://b.cloud.databricks.com"))
    with pytest.raises(WorkspaceConfigError, match="does not match"):
        assert_host_matches_sdk(ws, binding)


def test_wizard_stdin_answers_init_prompts_in_order() -> None:
    """Pins the scripted answers to the init wizard's prompt order (dev target only)."""
    scenario = Scenario(
        id="portfolio_assistant", kind="example", variant_id="portfolio_assistant", lane="canary", timeout_minutes=10
    )
    answers = wizard_stdin(scenario, project_name="af-pa-canary", host="https://h.example", catalog="cat")
    assert answers.splitlines() == [
        "af-pa-canary",
        "https://h.example",
        "cat",
        "",
        "n",
        "n",
        "git",
        "portfolio_assistant",
    ]


def test_readiness_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_foundry.testing.workspace import WorkspaceBinding

    binding = WorkspaceBinding(host="https://a.cloud.databricks.com", catalog="c")
    ws = SimpleNamespace(
        config=SimpleNamespace(host="https://a.cloud.databricks.com"),
        apps=SimpleNamespace(get=lambda _n: SimpleNamespace(app_status=SimpleNamespace(state="STOPPED"), url="")),
    )
    monkeypatch.setattr("agent_foundry.testing.readiness.time.sleep", lambda _s: None)
    times = iter([0.0, 0.0, 10.0])
    monkeypatch.setattr("agent_foundry.testing.readiness.time.monotonic", lambda: next(times, 10.0))
    with pytest.raises(ReadinessError):
        wait_for_app(ws, "app", binding=binding, timeout_seconds=5, poll_seconds=1)


def _running_ws() -> SimpleNamespace:
    app = SimpleNamespace(app_status=SimpleNamespace(state="RUNNING"), url="https://app.example/")
    return SimpleNamespace(
        apps=SimpleNamespace(get=lambda _n: app),
        config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer t"}),
    )


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://app.example", code, "status", Message(), None)


def _replay_urlopen(monkeypatch: pytest.MonkeyPatch, outcomes: list[int]) -> list[str]:
    """Patch ``urlopen`` to answer each probe with the next HTTP status; return the probed URLs."""
    probed: list[str] = []
    pending = iter(outcomes)

    def _urlopen(request: Any, timeout: float) -> Any:
        probed.append(request.full_url)
        code = next(pending)
        if code >= 400:
            raise _http_error(code)
        return BytesIO(b"ok")

    monkeypatch.setattr("agent_foundry.testing.readiness.urlopen", _urlopen)
    monkeypatch.setattr("agent_foundry.testing.readiness.time.sleep", lambda _s: None)
    return probed


def test_readiness_waits_until_proxy_reaches_app(monkeypatch: pytest.MonkeyPatch) -> None:
    probed = _replay_urlopen(monkeypatch, [502, 503, 200])

    assert wait_for_app(_running_ws(), "app", timeout_seconds=60, poll_seconds=1) == "https://app.example/"
    assert probed == ["https://app.example/health"] * 3


def test_readiness_accepts_any_non_gateway_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    probed = _replay_urlopen(monkeypatch, [404])

    assert wait_for_app(_running_ws(), "app", timeout_seconds=60, poll_seconds=1) == "https://app.example/"
    assert len(probed) == 1


def test_readiness_times_out_while_proxy_returns_502(monkeypatch: pytest.MonkeyPatch) -> None:
    _replay_urlopen(monkeypatch, [502, 502])
    times = iter([0.0, 0.0, 1.0, 10.0])
    monkeypatch.setattr("agent_foundry.testing.readiness.time.monotonic", lambda: next(times, 10.0))

    with pytest.raises(ReadinessError, match="proxy not reaching the App yet"):
        wait_for_app(_running_ws(), "app", timeout_seconds=5, poll_seconds=1)


def test_authenticated_post_json_sends_auth_and_extra_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def _urlopen(request: Any, timeout: float) -> Any:
        sent.update(headers=dict(request.header_items()), data=request.data, timeout=timeout)
        return BytesIO(b'{"output": []}')

    monkeypatch.setattr("agent_foundry.testing.readiness.urlopen", _urlopen)
    ws = SimpleNamespace(config=SimpleNamespace(authenticate=lambda: {"Authorization": "Bearer t"}))
    reply = authenticated_post_json(ws, "https://app/invocations", {"input": []}, headers={"X-Extra": "1"})
    assert reply == {"output": []}
    assert sent["headers"] == {"Authorization": "Bearer t", "Content-type": "application/json", "X-extra": "1"}
    assert sent["data"] == b'{"input": []}'


def test_destroy_runs_on_failure() -> None:
    destroyed: list[str] = []

    def _fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run_then_destroy(_fail, lambda: destroyed.append("yes"))
    assert destroyed == ["yes"]


def test_janitor_matches_ephemeral_names() -> None:
    found = matching_app_names(["dev-af-rm-abc123-app", "prod-customer-app", "dev-af-pa-ffffff-app", "unrelated"])
    assert found == ["dev-af-pa-ffffff-app", "dev-af-rm-abc123-app"]
