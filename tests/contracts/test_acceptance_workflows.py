"""Upstream vs downstream lifecycle tripwires."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]


ACCEPTANCE = REPO / ".github/workflows/acceptance.yml"
JOURNEY = REPO / ".github/actions/canary-journey/action.yml"
APP_CANARY_JOBS = ("brick", "payload", "example")
SMOKE_JOBS = ("payload", "example")
LIVE_JOBS = ("serving", "brick", "payload", "example")


def _job(workflow: str, name: str) -> str:
    start = workflow.index(f"\n  {name}:\n")
    following = [workflow.find(f"\n  {other}:\n", start + 1) for other in LIVE_JOBS]
    ends = [at for at in following if at > start]
    return workflow[start : min(ends)] if ends else workflow[start:]


def test_acceptance_workflow_is_valid_yaml() -> None:
    """Unindented lines inside ``run: |`` make GitHub drop the whole canary workflow."""
    yaml.safe_load(ACCEPTANCE.read_text(encoding="utf-8"))


def test_upstream_acceptance_rebuilds_then_keeps_success() -> None:
    ci = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    acceptance = ACCEPTANCE.read_text(encoding="utf-8")
    journey = JOURNEY.read_text(encoding="utf-8")
    assert "not live" in ci
    assert "branches: [main]" in acceptance
    assert "uses: ./.github/workflows/acceptance-matrix.yml" not in acceptance
    assert not (REPO / ".github/workflows/acceptance-matrix.yml").exists()
    assert journey.find("Rebuild previous canary") > journey.find("agent-foundry scaffold")
    assert "if: inputs.destroy-first == 'true'" in journey
    assert 'destroy-first: "true"' in _job(acceptance, "brick")
    assert "--force" in journey
    assert "Destroy (always)" not in acceptance
    assert "AGENT_FOUNDRY_RUN_ID: canary" in acceptance
    assert "max-parallel: 1" not in acceptance
    assert "schedule:" not in acceptance
    assert "vars.AGENT_FOUNDRY_TEST_CATALOG" in acceptance
    assert "the canary workspace" not in acceptance
    for text in (acceptance, journey):
        assert "actions/upload-artifact@v" not in text
        assert "databricks/setup-cli@v" not in text
    assert "databricks/setup-cli@" in journey
    assert "actions/upload-artifact@" in acceptance
    assert "uv run --no-sync python -m agent_foundry.testing.harness matrix --kind" in acceptance
    assert "python_version: ${{ env.PYTHON_VERSION }}" not in acceptance


def test_canary_levels_run_in_parallel_after_catalog() -> None:
    """App canaries do not share names, so they start together after catalog."""
    acceptance = ACCEPTANCE.read_text(encoding="utf-8")
    assert "name: Framework acceptance" in acceptance
    for name in LIVE_JOBS:
        assert "needs: catalog\n" in _job(acceptance, name), name
    payload = _job(acceptance, "payload")
    example = _job(acceptance, "example")
    assert "needs.catalog.outputs.payloads != '[]'" in payload
    assert "needs.catalog.outputs.examples != '[]'" in example
    assert "[catalog, brick]" not in payload
    assert "[catalog, brick, payload]" not in example


def test_brick_canary_checks_each_brick_then_destroys_and_checks_gone() -> None:
    brick = _job(ACCEPTANCE.read_text(encoding="utf-8"), "brick")
    order = [
        brick.find('--only "${bricks[$i]}"'),
        brick.find('"${checks[$i]}" -m live --run-live'),
        brick.find("agent-foundry destroy"),
        brick.find("-m gone --run-live --run-gone"),
    ]
    assert -1 not in order and order == sorted(order), order
    assert "--force" in brick


def _steps(job: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = yaml.safe_load(ACCEPTANCE.read_text(encoding="utf-8"))["jobs"][job]["steps"]
    return steps


def _split_at_smoke(steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    names = [step.get("name") for step in steps]
    assert "Smoke" in names
    cut = names.index("Smoke") + 1
    return steps[:cut], steps[cut:]


@pytest.mark.parametrize("name", SMOKE_JOBS)
def test_app_canary_gates_on_deploy_wait_smoke(name: str) -> None:
    """The gate is what a user runs: CLI verbs in the generated repo, no framework-checkout pytest."""
    gate, _ = _split_at_smoke(_steps(name))
    runs = [str(step.get("run", "")) for step in gate]
    order = [
        next(i for i, run in enumerate(runs) if "uv run agent-foundry deploy" in run),
        next(i for i, run in enumerate(runs) if "uv run agent-foundry wait" in run),
        next(i for i, run in enumerate(runs) if "uv run agent-foundry smoke" in run),
    ]
    assert order == sorted(order), order
    journey = next(step for step in gate if step.get("id") == "journey")
    assert journey["with"]["destroy-first"] == "${{ github.event.inputs.reset == 'true' }}"
    gate_text = yaml.safe_dump(gate)
    for removed in ("--run-live", "--no-sync", "prep.json", "AGENT_FOUNDRY_PROJECT_ROOT", "agent-foundry eval"):
        assert removed not in gate_text, removed


@pytest.mark.parametrize("name", APP_CANARY_JOBS)
def test_checkout_install_matches_framework_pytest_use(name: str) -> None:
    """Only jobs that run pytest from the framework checkout install it; the rest use the project's .venv."""
    steps = _steps(name)
    journey = next(step for step in steps if step.get("id") == "journey")
    installs = journey["with"].get("install-checkout", "true") == "true"
    runs_checkout_pytest = any("--no-sync python -m pytest" in str(step.get("run", "")) for step in steps)
    assert installs == runs_checkout_pytest, name


def test_journey_caches_uv_downloads() -> None:
    journey = JOURNEY.read_text(encoding="utf-8")
    assert "enable-cache: true" in journey
    assert "if: inputs.install-checkout == 'true'" in journey


@pytest.mark.parametrize("name", SMOKE_JOBS)
def test_steps_after_smoke_never_fail_the_canary(name: str) -> None:
    _, after = _split_at_smoke(_steps(name))
    report = [step for step in after if not str(step.get("uses", "")).startswith("actions/upload-artifact@")]
    assert all(step.get("continue-on-error") is True for step in report), report


def test_payload_canary_reports_tracing_and_evaluation() -> None:
    _, after = _split_at_smoke(_steps("payload"))
    runs = "\n".join(str(step.get("run", "")) for step in after)
    assert "payloads/agent/tests/live/test_tracing.py" in runs
    assert "payloads/agent/tests/live/test_observability.py" in runs
    assert 'uv run agent-foundry eval --instance "$PROJECT" --target dev' in runs


def test_brick_canary_builds_the_index_on_the_shared_endpoint() -> None:
    brick = _job(ACCEPTANCE.read_text(encoding="utf-8"), "brick")
    assert "AGENT_FOUNDRY_TEST_VS_ENDPOINT: ${{ vars.AGENT_FOUNDRY_TEST_VS_ENDPOINT }}" in brick
    assert '--vars "vs_endpoint_name=$AGENT_FOUNDRY_TEST_VS_ENDPOINT"' in brick
    assert "--vars source_table=docs" in brick
    assert "tomllib.loads((inst / 'foundry.toml').read_text(encoding='utf-8'))" in brick
    assert "tomllib.loads((inst / 'foundry.toml').read_bytes())" not in brick


def test_serving_live_calls_model_serving_not_the_app() -> None:
    serving = _job(ACCEPTANCE.read_text(encoding="utf-8"), "serving")
    assert "environment: framework-acceptance\n" in serving
    assert "canary-journey" not in serving
    assert "agent-foundry deploy" not in serving
    assert "\n    continue-on-error: true\n" in serving
    assert "payloads/agent/tests/live/runtime" in serving
    assert "FOUNDRY_REASONING_E2E_ENDPOINT: databricks-claude-sonnet-5-5" in serving
    assert "FOUNDRY_PROMPT_CACHE_E2E_ENDPOINT: databricks-claude-sonnet-4-5" in serving


def test_every_canary_job_is_reviewed_and_uses_secret_or_oidc() -> None:
    """Each live job waits on reviewers; auth is the environment's client secret, else GitHub OIDC."""
    acceptance = ACCEPTANCE.read_text(encoding="utf-8")
    assert "id-token: write" in acceptance
    for name in APP_CANARY_JOBS:
        job = _job(acceptance, name)
        assert "environment: framework-acceptance\n" in job, name
        assert "DATABRICKS_AUTH_TYPE: ${{ secrets.DATABRICKS_CLIENT_SECRET == '' && 'github-oidc' || '' }}" in job, name
        assert "DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}" in job, name
        assert "DATABRICKS_CLIENT_ID: ${{ secrets.DATABRICKS_CLIENT_ID }}" in job, name
        assert "DATABRICKS_CLIENT_SECRET: ${{ secrets.DATABRICKS_CLIENT_SECRET }}" in job, name
        assert 'AGENT_FOUNDRY_REQUIRE_LIVE: "1"' in job, name
        assert "uses: ./.github/actions/canary-journey" in job, name
    brick = _job(acceptance, "brick")
    assert 'AGENT_FOUNDRY_SKIP_DEFAULT_WORKSPACE_CLIENT: "false"' in brick
    assert brick.find("agent-foundry deploy") < brick.find("-m live --run-live")
    serving = _job(acceptance, "serving")
    assert "DATABRICKS_HOST: ${{ secrets.DATABRICKS_HOST }}" in serving
    assert 'AGENT_FOUNDRY_REQUIRE_LIVE: "1"' in serving
    assert acceptance.count("environment: framework-acceptance") == len(LIVE_JOBS)
    assert "DATABRICKS_TOKEN" not in acceptance


def test_upstream_acceptance_follows_consumer_uvx_journey() -> None:
    """Live acceptance consumes the candidate SHA the way downstream repos do."""
    acceptance = ACCEPTANCE.read_text(encoding="utf-8")
    journey = JOURNEY.read_text(encoding="utf-8")
    assert "github.event.pull_request.head.sha || github.sha" in acceptance
    assert "git+https://github.com/${{ github.repository }}.git@" in acceptance
    assert 'uvx --python "$PY" --from "$FRAMEWORK_SPEC" agent-foundry init' in journey
    assert "http.https://github.com/.extraheader" in journey
    assert "uv run agent-foundry destroy" in journey
    assert "uv run agent-foundry scaffold" in journey
    assert "uv run agent-foundry deploy" in acceptance
    assert "uv run agent-foundry wait" in acceptance
    assert 'uv run agent-foundry smoke --instance "$PROJECT" --target dev' in _job(acceptance, "example")
    for removed in (
        "uv build --wheel",
        "download-artifact",
        ".venv-accept",
        "harness overlay",
        "--no-lock",
        "--no-sync agent-foundry",
    ):
        assert removed not in acceptance, removed
        assert removed not in journey, removed
    assert not (REPO / "agent_foundry/testing/overlay.py").exists()


def test_journey_keeps_inputs_out_of_shell_source() -> None:
    """Composite inputs reach ``run:`` only through env, never as ``${{ }}`` in the script."""
    journey = JOURNEY.read_text(encoding="utf-8")
    scripts = journey.split("run: |")[1:]
    for script in scripts:
        body = script.split("\n    - ", 1)[0]
        assert "${{" not in body


def test_ci_has_no_workspace_lane() -> None:
    """Live checks run only in the canaries, against a CLI deployment."""
    ci = (REPO / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "integration-tests:" not in ci
    assert "run-integration" not in ci
    assert "--run-live" not in ci
    assert "environment: framework-acceptance" not in ci
    assert "agent-foundry validate" in ci
    assert not (REPO / "tests/integration").exists()
    assert not (REPO / ".github/workflows/deploy.yml").exists()


def test_janitor_is_report_only() -> None:
    janitor = (REPO / ".github/workflows/acceptance-janitor.yml").read_text(encoding="utf-8")
    src = (REPO / "agent_foundry/testing/janitor.py").read_text(encoding="utf-8")
    assert "report-only" in janitor.lower() or "Report leaked" in janitor
    assert "deletion is disabled" in src
